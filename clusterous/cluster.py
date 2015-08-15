import subprocess
import tempfile
import sys
import os
import yaml
import logging
import time
import glob
import shutil
import json
import stat
from datetime import datetime
from dateutil import parser


from dateutil.relativedelta import relativedelta
import boto.ec2
import boto.s3.connection
import paramiko

import defaults
from defaults import get_script
from helpers import AnsibleHelper, SSHTunnel, NoWorkingClusterException

# TODO: Move to another module as appropriate, as this is very general purpose
def retry_till_true(func, sleep_interval, timeout_secs=300):
    """
    Call func repeatedly, with an interval of sleep_interval, for up to
    timeout_secs seconds, until func returns true.

    Returns true if succesful, false if timeout occurs
    """
    success = True
    start_time = time.time()
    while not func():
        if time.time() >= start_time + timeout_secs:
            success = False
            break
        time.sleep(sleep_interval)

    return success

class Cluster(object):
    """
    Represents infrastrucure aspects of the cluster. Includes high level operations
    for setting up cluster controller, launching application nodes etc.

    Prepares cluster to a stage where applications can be run on it
    """
    def __init__(self, config, cluster_name=None, cluster_name_required=True):
        self._config = config
        self._running = False
        self._logger = logging.getLogger(__name__)
        self._controller_ip = ''
        if cluster_name_required and not cluster_name:
            name = self._get_working_cluster_name()
            if not name:
                message = 'No working cluster has been set.'
                self._logger.error(message)
                raise NoWorkingClusterException(message)
            self.cluster_name = name
        elif cluster_name:
            self.cluster_name = cluster_name


    def _get_cluster_info(self):
        cluster_info_file = os.path.expanduser(defaults.CLUSTER_INFO_FILE)
        if not os.path.isfile(cluster_info_file):
            return None

        f = open(os.path.expanduser(cluster_info_file), 'r')
        cluster_info = yaml.load(f)
        return cluster_info


    def _get_working_cluster_name(self):
        cluster_info = self._get_cluster_info()
        if not cluster_info:
            return None
        return cluster_info.get('cluster_name')


    def _get_controller_ip(self):
        if self._controller_ip:
            return self._controller_ip

        info = self._get_cluster_info()
        if not 'controller' in info or not 'ip' in info['controller']:
            return None
        ip = info['controller']['ip']
        self._controller_ip = ip

        return ip

    def controller_tunnel(self, remote_port):
        """
        Returns helpers.SSHTunnel object connected to remote_port on controller
        """
        pass

    def init_cluster(self, cluster_name):
        pass

    def launch_nodes(self, num_nodes, instance_type):
        pass

    def _ssh_to_controller(self):
        raise NotImplementedError('SSH connections are specific to cloud providers')


class AWSCluster(Cluster):

    def _controller_vars_dict(self):
        return {
                'AWS_KEY': self._config['access_key_id'],
                'AWS_SECRET': self._config['secret_access_key'],
                'clusterous_s3_bucket': self._config['clusterous_s3_bucket'],
                'registry_s3_path': defaults.registry_s3_path,
                'remote_scripts_dir': defaults.get_remote_dir(),
                'remote_host_scripts_dir': defaults.remote_host_scripts_dir
                }

    def _ansible_env_credentials(self):
        return {
                'AWS_ACCESS_KEY_ID': self._config['access_key_id'],
                'AWS_SECRET_ACCESS_KEY': self._config['secret_access_key']
                }

    def _make_vars_file(self, vars_dict):
        if not vars_dict:
            vars_d = {}
        else:
            vars_d = vars_dict
        vars_file = tempfile.NamedTemporaryFile()
        vars_file.write(yaml.dump(vars_d, default_flow_style=False))
        vars_file.flush()
        return vars_file

    def _run_on_controller(self, playbook, hosts_file):

        local_vars = {
                'key_file_src': self._config['key_file'],
                'key_file_name': defaults.remote_host_key_file,
                'hosts_file_src': hosts_file,
                'hosts_file_name': os.path.basename(hosts_file),
                'remote_dir': defaults.remote_host_scripts_dir,
                'playbook_file': playbook
                }

        local_vars_file = self._make_vars_file(local_vars)

        AnsibleHelper.run_playbook(get_script('ansible/run_remote.yml'),
                      local_vars_file.name, self._config['key_file'],
                      hosts_file=os.path.expanduser(defaults.current_controller_ip_file))

        local_vars_file.close()

    def _get_instances(self, cluster_name):
        conn = boto.ec2.connect_to_region(self._config['region'],
                    aws_access_key_id=self._config['access_key_id'],
                    aws_secret_access_key=self._config['secret_access_key'])

        # Delete instances
        instance_filters = { 'tag:{0}'.format(defaults.instance_tag_key):
                        [cluster_name],
                        'instance-state-name': ['running', 'pending']
                        }
        instance_list = conn.get_only_instances(filters=instance_filters)
        return instance_list

    def _ssh_to_controller(self):
        try:
            ssh = paramiko.SSHClient()
            ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            ssh.connect(hostname = self._get_controller_ip(),
                        username = 'root',
                        key_filename = os.path.expanduser(self._config['key_file']))
        except paramiko.AuthenticationException as e:
            self._logger.error('Could not connect to controller')
            raise e

        return ssh


    def make_controller_tunnel(self, remote_port):
        """
        Returns helpers.SSHTunnel object connected to remote_port on controller
        """
        return SSHTunnel(self._get_controller_ip(), 'root',
                os.path.expanduser(self._config['key_file']), remote_port)

    def make_tunnel_on_controller(self, controller_port, remote_host, remote_port):
        """
        Create an ssh tunnel from the controller to a cluster node. Note that this
        doesn't expose a port on the local machine
        """
        remote_key_path = '/root/{0}'.format(os.path.basename(self._config['key_file']))

        ssh_sock_file = '/tmp/clusterous_tunnel_%h_{0}.sock'.format(controller_port)
        create_cmd = ('ssh -4 -i {0} -f -N -M -S {1} -o ExitOnForwardFailure=yes ' +
              '-o StrictHostKeyChecking=no ' +
              'root@{2} -L {3}:127.0.0.1:{4}').format(remote_key_path,
              ssh_sock_file, remote_host, controller_port,
              remote_port)


        destroy_cmd = 'ssh -S {0} -O exit {1}'.format(ssh_sock_file, remote_host)

        ssh = self._ssh_to_controller()
        sftp = ssh.open_sftp()
        sftp.put(os.path.expanduser(self._config['key_file']), remote_key_path)
        sftp.chmod(remote_key_path, stat.S_IRUSR | stat.S_IWUSR)
        sftp.close()

        # First ensure that any previously created tunnel is destroyed
        stdin, stdout, stderr = ssh.exec_command(destroy_cmd)
        # Create tunnel
        stdin, stdout, stderr = ssh.exec_command(create_cmd)

        ssh.close()

    def create_permanent_tunnel_to_controller(self, remote_port, local_port, prefix=''):
        """
        Creates a persistent SSH tunnel from local machine to controller by running
        the ssh command in the background
        """

        key_file = os.path.expanduser(self._config['key_file'])

        # Useful to isolate user created sockets from our own
        prefix_str = 'clusterous' if not prefix else prefix

        # Temporary file containing ssh socket data
        ssh_sock_file = '{0}/{1}_tunnel_%h_{2}.sock'.format(
                        os.path.expanduser(defaults.local_session_data_dir),
                        prefix_str, local_port)

        # Ensure that any previously created tunnel is destroyed
        reset_cmd = ['ssh', '-S', ssh_sock_file, '-O', 'exit',
                self._get_controller_ip()]

        # Normal tunnel command
        connect_cmd = ['ssh', '-i', key_file, '-N', '-f', '-M',
                '-S', ssh_sock_file, '-o', 'ExitOnForwardFailure=yes',
                'root@{0}'.format(self._get_controller_ip()),
                '-L', '{0}:127.0.0.1:{1}'.format(local_port, remote_port)]

        # If socket file doesn't exist, it will return with an error. This is normal
        process = subprocess.Popen(reset_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=None)
        output, error = process.communicate()

        return_code = subprocess.call(connect_cmd)

        return True if return_code == 0 else False

    def delete_all_permanent_tunnels(self):
        """
        Deletes all persistent SSH tunnels to the controller that were created by
        create_permanent_tunnel_to_controller()
        """
        key_file = os.path.expanduser(self._config['key_file'])

        sock_files = glob.glob('{0}/clusterous_tunnel_*.sock'.format(
                        os.path.expanduser(defaults.local_session_data_dir)))

        all_deleted = True
        for sock in sock_files:
            reset_cmd = ['ssh', '-S', sock, '-O', 'exit',
                            self._get_controller_ip()]
            process = subprocess.Popen(reset_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=None)
            output, error = process.communicate()

            if process.returncode != 0:
                self._logger.warning('Problem deleting SSH tunnel at {0}'.format(sock))
                all_deleted = False

        return all_deleted

    def _create_security_group(self, cluster_name, conn):
        """
        Given a cluster name and Boto EC2 connection, creates a
        cluster security group (before deleting any existing one)
        and returns the SG id
        """
        sg_name = defaults.security_group_name_format.format(cluster_name)
        descr = 'Security group for {0}'.format(cluster_name)
        for s in conn.get_all_security_groups():
            if s.name == sg_name:
                self._logger.debug('First deleting existing security group "{0}"'.format(sg_name))
                s.delete()
        sg = conn.create_security_group(sg_name, descr, vpc_id = self._config['vpc_id'])
        sg.add_tag(key='Name', value=sg_name)
        sg.add_tag(key=defaults.instance_tag_key, value=cluster_name)
        sg.authorize(ip_protocol='tcp', from_port='22', to_port='22', cidr_ip='0.0.0.0/0')
        sg.authorize(ip_protocol='tcp', from_port='80', to_port='80', cidr_ip='0.0.0.0/0')
        # Boto documentation is misleading: need to specify protocol to -1 to mean all protocols
        sg.authorize(ip_protocol=-1, src_group=sg)
        return sg.id

    def _wait_and_tag_instance_reservations(self, tag_and_inst_list, sleep_interval=5):
        launched = {}
        launched_ids = {}
        inst_list = []
        inst_to_tag = {}
        for (label, tag, insts) in tag_and_inst_list:
            inst_list.extend(insts)
            for i in insts:
                inst_to_tag[i.id] = (label, tag)

        while len(launched_ids) < len(inst_list):
            time.sleep(sleep_interval)
            for inst in inst_list:
                if inst.state == 'running' and inst.id not in launched_ids:
                    if inst.ip_address:
                        launched_ids[inst.id] = True
                        label, tags = inst_to_tag[inst.id]
                        # Add default "clusterous" tag
                        t = tags.copy()
                        clusterous_tag = {defaults.instance_tag_key: self.cluster_name}
                        t.update(clusterous_tag)
                        inst.add_tags(t)
                        if not label in launched:
                            launched[label] = []
                        launched[label].append(inst.ip_address)
                        self._logger.debug('Running {0} {1} {2}'.format(inst.ip_address, tags, inst.id))
                # There is no good reason for this to happen in practice
                elif inst.state in ('terminated', 'stopped', 'stopping'):
                    self._logger.error('Instance {0} is in state "{1}"'.format(inst.id, inst.state))
                    self.logger.error('Problem creating instance')
                    # Unrecoverable error, exit to prevent infinite loop
                    return None
                else:
                    # Refresh instance data
                    inst.update()

        return launched

    def _write_to_hosts_file(self, filename, ips, group_name='', overwrite=False):
        """
        Given a destination absolute filename, a list of IPs, and a group name,
        create an Ansible inventory file, returning True on success
        """
        mode = 'w' if overwrite else 'a'
        with open(filename, mode) as f:
            if group_name:
                f.write('[{0}]\n'.format(group_name.strip()))
            for ip in ips:
                f.write('{0}\n'.format(ip.strip()))
        return True

    def init_cluster(self, cluster_name, nodes_info=[]):
        """
        Initialise security group(s), cluster controller etc
        """
        self.cluster_name = cluster_name

        # Create dirs
        for directory in [defaults.local_config_dir, defaults.local_session_data_dir]:
            d = os.path.expanduser(directory)
            if not os.path.exists(d):
                os.makedirs(d)

        c = self._config

        # Create registry bucket if it doesn't already exist
        s3conn = boto.s3.connection.S3Connection(c['access_key_id'], c['secret_access_key'])
        if not s3conn.lookup(c['clusterous_s3_bucket']):
            s3conn.create_bucket(c['clusterous_s3_bucket'], location=c['region'])


        conn = boto.ec2.connect_to_region(c['region'], aws_access_key_id=c['access_key_id'],
                                            aws_secret_access_key=c['secret_access_key'])


        # Create Security group
        self._logger.info('Creating security group')
        sg_id = self._create_security_group(cluster_name, conn)

        # Launch all instances
        controller_tags_and_res = []

        # Launch controller
        self._logger.info('Starting controller')
        block_devices = boto.ec2.blockdevicemapping.BlockDeviceMapping(conn)
        root_vol = boto.ec2.blockdevicemapping.BlockDeviceType(connection=conn, delete_on_termination=True)
        root_vol.size = 20

        block_devices['/dev/sda1'] = root_vol
        controller_res = conn.run_instances(defaults.controller_ami_id, min_count=1,
                                        key_name=c['key_pair'], instance_type=defaults.controller_instance_type,
                                        subnet_id=c['subnet_id'], block_device_map=block_devices, security_group_ids=[sg_id])

        controller_tags = {'Name': defaults.controller_name_format.format(cluster_name)}
        controller_tags_and_res.append(('controller', controller_tags, controller_res.instances))

        # Loop through node groups and launch all
        self._logger.info('Starting all nodes')
        node_tags_and_res = []
        for num_nodes, instance_type, node_tag in nodes_info:
            res = conn.run_instances(defaults.node_ami_id, min_count=num_nodes, max_count=num_nodes,
                                key_name=c['key_pair'], instance_type=instance_type,
                                subnet_id=c['subnet_id'], security_group_ids=[sg_id])
            node_tags = {'Name': defaults.node_name_format.format(cluster_name, node_tag)}
            node_tags_and_res.append((node_tag, node_tags, res.instances))


        # Wait for controller to launch
        controller = self._wait_and_tag_instance_reservations(controller_tags_and_res)

        # Create and attach shared volume
        self._logger.info('Creating shared volume')
        shared_vol = conn.create_volume(20, zone=controller_res.instances[0].placement)
        while shared_vol.status != 'available':
            time.sleep(2)
            shared_vol.update()
        self._logger.debug('Shared volume {0} created'.format(shared_vol.id))
        conn.create_tags([shared_vol.id], {'Name': defaults.controller_name_format.format(cluster_name),
                                           defaults.instance_tag_key: cluster_name})

        attach = shared_vol.attach(controller_res.instances[0].id, '/dev/sdf')
        while shared_vol.attachment_state() != 'attached':
            time.sleep(3)
            shared_vol.update()

        # Configure controller
        controller_inventory = os.path.expanduser(defaults.current_controller_ip_file)
        self._write_to_hosts_file(controller_inventory, [controller.values()[0][0]], 'controller', overwrite=True)

        controller_vars_dict = self._controller_vars_dict()
        controller_vars_file = self._make_vars_file(controller_vars_dict)

        self._logger.info('Configuring controller instance...')
        AnsibleHelper.run_playbook(get_script('ansible/01_configure_controller.yml'),
                                   controller_vars_file.name, self._config['key_file'],
                                   hosts_file=controller_inventory)

        controller_vars_file.close()

        # Configure nodes
        if node_tags_and_res:
            self._configure_nodes(nodes_info, node_tags_and_res, controller.values()[0][0])

        # Create cluster info file
        self._set_cluster_info(cluster_name, controller.values()[0][0])

        # TODO: this is useful for debugging, but remove at a later stage
        self.create_permanent_tunnel_to_controller(8080, 8080, prefix='marathon')


    def _configure_nodes(self, nodes_info, node_tags_and_res, controller_ip):
        nodes = self._wait_and_tag_instance_reservations(node_tags_and_res)

        nodes_inventory = tempfile.NamedTemporaryFile()
        for num_nodes, instance_type, node_tag in nodes_info:
            self._write_to_hosts_file(nodes_inventory.name, nodes[node_tag], node_tag, overwrite=False)
        nodes_inventory.flush()
        self._logger.info('Configuring nodes...')
        self._run_on_controller('configure_nodes.yml', nodes_inventory.name)
        nodes_inventory.close()
        return

    def _set_cluster_info(self, cluster_name, controller_ip):
        """
        Writes cluster information to cluster info file in YAML format
        """
        data = {}
        data['controller'] = {'ip': controller_ip}
        data['cluster_name'] = cluster_name

        # Write cluster_info
        cluster_info_file = os.path.expanduser(defaults.CLUSTER_INFO_FILE)
        with open(cluster_info_file,'w+') as f:
            f.write(yaml.dump(data, default_flow_style=False))

        return True

    def docker_build_image(self, full_path, image_name):
        """
        Create a new docker image
        """

        vars_dict = {
                'cluster_name': self.cluster_name,
                'dockerfile_path': os.path.dirname(full_path),
                'dockerfile_folder': os.path.basename(full_path),
                'image_name': image_name,
                }

        if not os.path.isdir(full_path):
            self._logger.error("Folder '{0}' does not exist".format(full_path))
            return False

        if not os.path.exists("{0}/Dockerfile".format(full_path)):
            self._logger.error("Folder '{0}' does not have a Dockerfile".format(full_path))
            return False

        vars_file = self._make_vars_file(vars_dict)
        self._logger.info('Started building docker image {0}'.format(image_name))
        AnsibleHelper.run_playbook(defaults.get_script('ansible/docker_01_build_image.yml'),
                                   vars_file.name,
                                   self._config['key_file'],
                                   env=self._ansible_env_credentials(),
                                   hosts_file=os.path.expanduser(defaults.current_controller_ip_file))
        vars_file.close()
        self._logger.info('Finished building docker image')
        return True

    def docker_image_info(self, image_name_str):
        """
        Gets information of a Docker image
        """
        if ':' in image_name_str:
            image_name, tag_name = image_name_str.split(':', 1)
        else:
            image_name = image_name_str
            tag_name = 'latest'

        image_info = {}
        # TODO: rewrite to use make HTTP calls directly
        with paramiko.SSHClient() as ssh:
            ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            ssh.connect(hostname = self._get_controller_ip(),
                        username = 'root',
                        key_filename = os.path.expanduser(self._config['key_file']))

            # get image_id
            cmd = 'curl registry:5000/v1/repositories/library/{0}/tags/{1}'.format(image_name, tag_name)
            stdin, stdout, stderr = ssh.exec_command(cmd)
            image_id = stdout.read().replace('"','')
            if 'Tag not found' in image_id:
                return None

            # get image_info
            cmd = 'curl registry:5000/v1/images/{0}/json'.format(image_id)
            stdin, stdout, stderr = ssh.exec_command(cmd)
            json_results = json.loads(stdout.read())
            image_info = { 'image_name': image_name,
                           'tag_name': tag_name,
                           'image_id': image_id,
                           'author': json_results.get('author',''),
                           'created': json_results.get('created','')
                           }
        return image_info

    def sync_put(self, local_path, remote_path):
        """
        Sync local folder to the cluster
        """
        # Check local path
        src_path = os.path.abspath(local_path)
        if not os.path.isdir(src_path):
            message = "Folder '{0}' does not exist".format(src_path)
            return (False, message)

        dst_path = '/home/data/{0}'.format(remote_path)
        vars_dict={
                'src_path': src_path,
                'dst_path': dst_path,
                }
        vars_file = self._make_vars_file(vars_dict)
        self._logger.debug('Started sync folder')
        AnsibleHelper.run_playbook(defaults.get_script('ansible/file_01_sync_put.yml'),
                                   vars_file.name,
                                   self._config['key_file'],
                                   env=self._ansible_env_credentials(),
                                   hosts_file=os.path.expanduser(defaults.current_controller_ip_file))
        vars_file.close()
        self._logger.debug('Finished sync folder')
        return (True, 'Ok')


    def sync_get(self, local_path, remote_path):
        """
        Sync folder from the cluster to local
        """
        # Check local path
        dst_path = os.path.abspath(local_path)
        if not os.path.isdir(dst_path):
            message = "Folder '{0}' does not exist".format(dst_path)
            return (False, message)

        # Check remote path
        remote_path = '/home/data/{0}'.format(remote_path)
        with paramiko.SSHClient() as ssh:
            ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            ssh.connect(hostname = self._get_controller_ip(), username = 'root',
                        key_filename = os.path.expanduser(self._config['key_file']))

            # check if folder exists
            cmd = "ls -d '{0}'".format(remote_path)
            stdin, stdout, stderr = ssh.exec_command(cmd)
            output_content = stdout.read()
            if 'cannot access' in stderr.read():
                message = "Error: Folder '{0}' does not exists.".format(remote_path)
                return (False, message)

        src_path = remote_path
        vars_dict={
                'src_path': src_path,
                'dst_path': dst_path,
                }
        vars_file = self._make_vars_file(vars_dict)
        self._logger.debug('Started sync folder')
        AnsibleHelper.run_playbook(defaults.get_script('ansible/file_02_sync_get.yml'),
                                   vars_file.name,
                                   self._config['key_file'],
                                   env=self._ansible_env_credentials(),
                                   hosts_file=os.path.expanduser(defaults.current_controller_ip_file))
        vars_file.close()
        self._logger.debug('Finished sync folder')
        return (True, 'Ok')

    def ls(self, remote_path):
        """
        List content of a folder on the on cluster
        """
        with paramiko.SSHClient() as ssh:
            ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            ssh.connect(hostname = self._get_controller_ip(), username = 'root',
                        key_filename = os.path.expanduser(self._config['key_file']))

            remote_path = '/home/data/{0}'.format(remote_path)
            cmd = "ls -al '{0}'".format(remote_path)
            stdin, stdout, stderr = ssh.exec_command(cmd)
            output_content = stdout.read()
            if 'cannot access' in stderr.read():
                message = "Error: Folder '{0}' does not exists.".format(remote_path)
                return (False, message)

            return (True, output_content)


    def rm(self, remote_path):
        """
        Delete content of a folder on the on cluster
        """
        with paramiko.SSHClient() as ssh:
            ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            ssh.connect(hostname = self._get_controller_ip(), username = 'root',
                        key_filename = os.path.expanduser(self._config['key_file']))

            # check if folder exists
            remote_path = '/home/data/{0}'.format(remote_path)
            cmd = "ls -d '{0}'".format(remote_path)
            stdin, stdout, stderr = ssh.exec_command(cmd)
            output_content = stdout.read()
            if 'cannot access' in stderr.read():
                message = "Error: Folder '{0}' does not exists.".format(remote_path)
                return (False, message)

            cmd = "rm -fr '{0}'".format(remote_path)
            stdin, stdout, stderr = ssh.exec_command(cmd)
            output_content = stdout.read()
            # TODO: More error checking may need to be added
            if 'cannot access' in stderr.read():
                message = "Error: Failed to delete folder '{0}'.".format(remote_path)
                return (False, message)

            return (True, 'Ok')


    def workon(self):
        """
        Sets a working cluster
        """
        # Getting cluster info
        instances = self._get_instances(self.cluster_name)
        controller_ip = None
        cluster_name = None
        for instance in instances:
            if defaults.controller_name_format.format(self.cluster_name) in instance.tags['Name']:
                controller_ip = instance.ip_address
                cluster_name = self.cluster_name

        if not controller_ip:
            return (False, 'Cluster "{0}" does not exist'.format(self.cluster_name))

        self._set_cluster_info(cluster_name, controller_ip)

        # Write controller_ip
        ip_file = os.path.expanduser(defaults.current_controller_ip_file)
        self._write_to_hosts_file(ip_file, [controller_ip], 'controller', overwrite=True)

        return (True, 'Ok')

    def info_status(self):
        info = {'name': self._get_working_cluster_name(),
                'up_time': '',
                'controller_ip': '',
                }
        instances = self._get_instances(self.cluster_name)
        for instance in instances:
            if defaults.controller_name_format.format(self.cluster_name) in instance.tags['Name']:
                info['controller_ip'] = str(instance.ip_address)
                launch_time = parser.parse(instance.launch_time)
                info['up_time'] = relativedelta(datetime.now(launch_time.tzinfo), launch_time)
        return info

    def info_instances(self):
        info = {}
        instances = self._get_instances(self.cluster_name)
        for instance in instances:
            if instance.instance_type not in info:
                info[instance.instance_type] = 0
            info[instance.instance_type] += 1
        return info

    def connect_to_container(self, component_name):
        '''
        Connects to a docker container and gets an interactive shell
        '''
        key_file_local = os.path.expanduser(self._config['key_file'])
        key_file_remote = '/root/{0}/{1}'.format(defaults.remote_host_scripts_dir, defaults.remote_host_key_file)
        container_id_script_local = defaults.get_script(defaults.container_id_script_file)
        container_id_script_remote = '/root/{0}/{1}'.format(defaults.remote_host_scripts_dir,defaults.container_id_script_file)
        container_id_script_node = '/tmp/{0}'.format(defaults.container_id_script_file)
        node = '{0}.marathon.mesos'.format(component_name)

        # SSH controller
        with self._ssh_to_controller() as ssh:
            self._logger.info("Connecting to '{0}' component".format(component_name))
            # Copy files to controller
            sftp = ssh.open_sftp()
            sftp.put(key_file_local, key_file_remote)
            sftp.put(container_id_script_local, container_id_script_remote)
            sftp.chmod(key_file_remote, stat.S_IRUSR | stat.S_IWUSR)
            sftp.close()

            def _retry(cmd):
                retry = 0
                while retry < 3:
                    stdin, stdout, stderr = ssh.exec_command(cmd)
                    if not stderr.readlines():
                        break
                    retry += 1
                    self._logger.info('Retry: {0}'.format(retry))
                    time.sleep(3)
                return (retry <3 ), stdout

            # Copy script to node
            cmd='scp -i {0} -oStrictHostKeyChecking=no {1} {2}:{3}'.format(key_file_remote,
                                                                           container_id_script_remote, node, container_id_script_node)
            success, stdout = _retry(cmd)
            if not success:
                self._logger.debug("Failed to copy scripts to controller")
                message = "Failed to connect to '{0}' component, try later".format(component_name)
                return (False, message)

            # Get container id
            cmd='ssh -i {0} -oStrictHostKeyChecking=no {1} source {2} {3}'.format(key_file_remote,
                                                                                  node, container_id_script_node, component_name)
            success, stdout = _retry(cmd)
            if not success:
                self._logger.debug("Failed to get container id for '{0}' component".format(component_name))
                message = "Failed to connect to '{0}' component, try later".format(component_name)
                return (False, message)

            container_id = stdout.readline().replace('\n','')

        # Shell
        node = '{0}.marathon.mesos'.format(component_name)
        cmd='ssh -i {0} -oStrictHostKeyChecking=no -A -t root@{1} \
             ssh -i {2} -oStrictHostKeyChecking=no -A -t {3} \
             docker exec -ti {4} bash'.format(key_file_local, self._get_controller_ip(),
                         key_file_remote, node, container_id)
        os.system(cmd)

        # Remove keys
        with self._ssh_to_controller() as ssh:
            cmd='rm -fr {0}'.format(key_file_remote)
            success, stdout = _retry(cmd)
            if not success:
                message = 'Failed to remove keys from controller'
                self._logger.debug(message)
                return (False, message)

        return (True, 'Ok')

    def info_shared_volume(self):
        info = {'total': '',
                'used': '',
                'used_pct': '',
                'free': ''}
        with paramiko.SSHClient() as ssh:
            ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            ssh.connect(hostname = self._get_controller_ip(),
                        username = 'root',
                        key_filename = os.path.expanduser(self._config['key_file']))
            cmd = 'df -h |grep {0}'.format(defaults.shared_volume_path[:-1])
            stdin, stdout, stderr = ssh.exec_command(cmd)
            volume_info = ' '.join(stdout.read().split()).split()
            if volume_info:
                info['total'] = volume_info[1]
                info['used'] =  volume_info[2]
                info['used_pct'] = volume_info[4]
                info['free'] = volume_info[3]
        return info

    def terminate_cluster(self):
        conn = boto.ec2.connect_to_region(self._config['region'],
                    aws_access_key_id=self._config['access_key_id'],
                    aws_secret_access_key=self._config['secret_access_key'])
        instance_list = self._get_instances(self.cluster_name)
        num_instances = len(instance_list)
        instances = [ i.id for i in instance_list ]

        self._logger.info('Terminating {0} instances'.format(num_instances))
        conn.terminate_instances(instance_ids=instances)

        def instances_terminated():
            term_filter = {'instance-state-name': 'terminated'}
            num_terminated = len(conn.get_only_instances(instance_ids=instances, filters=term_filter))
            return num_terminated == num_instances

        success = retry_till_true(instances_terminated, 2)
        if not success:
            self._logger.error('Timeout while trying to terminate instances in {0}'.format(self.cluster_name))
        else:
            self._logger.debug('{0} instances terminated'.format(num_instances))

        # Delete EBS volume
        volumes = conn.get_all_volumes(filters={'tag:{0}'.format(defaults.instance_tag_key):self.cluster_name})
        volumes_deleted = [ v.delete() for v in volumes ]
        volume_ids_str = ','.join([ v.id for v in volumes])
        if False in volumes_deleted:
            self._logger.error('Unable to delete volume in {0}: {1}'.format(self.cluster_name, volume_ids_str))
        else:
            self._logger.debug('Deleted shared volume: {0}'.format(volume_ids_str))

        # Delete security group
        sg = conn.get_all_security_groups(filters={'tag:{0}'.format(defaults.instance_tag_key):self.cluster_name})
        sg_deleted = [ g.delete() for g in sg ]
        if False in sg_deleted:
            self._logger.error('Unable to delete security group for {0}'.format(self.cluster_name))
        else:
            self._logger.debug('Deleted security group')

        # Delete cluster info
        os.remove(os.path.expanduser(defaults.CLUSTER_INFO_FILE))
        os.remove(os.path.expanduser(defaults.current_controller_ip_file))
        if os.path.exists(os.path.expanduser(defaults.local_session_data_dir)):
            shutil.rmtree(os.path.expanduser(defaults.local_session_data_dir))

        return True
