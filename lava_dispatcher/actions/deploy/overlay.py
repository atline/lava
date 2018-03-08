# Copyright (C) 2014 Linaro Limited
#
# Author: Neil Williams <neil.williams@linaro.org>
#
# This file is part of LAVA Dispatcher.
#
# LAVA Dispatcher is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.
#
# LAVA Dispatcher is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along
# with this program; if not, see <http://www.gnu.org/licenses>.

import os
import stat
import glob
import shutil
import tarfile
from lava_dispatcher.actions.deploy import DeployAction
from lava_dispatcher.action import (
    Action,
    InfrastructureError,
    LAVABug,
    Pipeline
)
from lava_dispatcher.actions.deploy.testdef import (
    TestDefinitionAction,
    get_test_action_namespaces,
)
from lava_dispatcher.utils.contextmanager import chdir
from lava_dispatcher.utils.filesystem import check_ssh_identity_file
from lava_dispatcher.utils.shell import infrastructure_error
from lava_dispatcher.utils.network import rpcinfo_nfs
from lava_dispatcher.protocols.multinode import MultinodeProtocol
from lava_dispatcher.protocols.vland import VlandProtocol


# pylint: disable=too-many-instance-attributes
class OverlayAction(DeployAction):
    """
    Creates a temporary location into which the lava test shell scripts are installed.
    The location remains available for the testdef actions to populate
    Multinode and LMP actions also populate the one location.
    CreateOverlay then creates a tarball of that location in the output directory
    of the job and removes the temporary location.
    ApplyOverlay extracts that tarball onto the image.

    Deployments which are for a job containing a 'test' action will have
    a TestDefinitionAction added to the job pipeline by this Action.

    The resulting overlay needs to be applied separately and custom classes
    exist for particular deployments, so that the overlay can be applied
    whilst the image is still mounted etc.

    This class handles parts of the overlay which are independent
    of the content of the test definitions themselves. Other
    overlays are handled by TestDefinitionAction.
    """

    name = "lava-overlay"
    description = "add lava scripts during deployment for test shell use"
    summary = "overlay the lava support scripts"

    def __init__(self):
        super(OverlayAction, self).__init__()
        self.lava_test_dir = os.path.realpath(
            '%s/../../lava_test_shell' % os.path.dirname(__file__))
        self.scripts_to_copy = []
        # 755 file permissions
        self.xmod = stat.S_IRWXU | stat.S_IXGRP | stat.S_IRGRP | stat.S_IXOTH | stat.S_IROTH
        self.target_mac = ''
        self.target_ip = ''
        self.probe_ip = ''
        self.probe_channel = ''

    def validate(self):
        super(OverlayAction, self).validate()
        self.scripts_to_copy = sorted(glob.glob(os.path.join(self.lava_test_dir, 'lava-*')))
        # Distro-specific scripts override the generic ones
        if not self.test_needs_overlay(self.parameters):
            return
        lava_test_results_dir = self.parameters['deployment_data']['lava_test_results_dir']
        lava_test_results_dir = lava_test_results_dir % self.job.job_id
        self.set_namespace_data(action='test', label='results', key='lava_test_results_dir',
                                value=lava_test_results_dir)
        lava_test_sh_cmd = self.parameters['deployment_data']['lava_test_sh_cmd']
        self.set_namespace_data(action='test', label='shared', key='lava_test_sh_cmd',
                                value=lava_test_sh_cmd)

        # Add distro support scripts
        distro = self.parameters['deployment_data']['distro']
        distro_support_dir = '%s/distro/%s' % (self.lava_test_dir, distro)
        self.scripts_to_copy += sorted(glob.glob(os.path.join(distro_support_dir,
                                                              'lava-*')))

        if not self.scripts_to_copy:
            self.errors = "Unable to locate lava_test_shell support scripts."
        if self.job.parameters.get('output_dir', None) is None:
            self.errors = "Unable to use output directory."
        if 'parameters' in self.job.device:
            if 'interfaces' in self.job.device['parameters']:
                if 'target' in self.job.device['parameters']['interfaces']:
                    self.target_mac = self.job.device['parameters']['interfaces']['target'].get('mac', '')
                    self.target_ip = self.job.device['parameters']['interfaces']['target'].get('ip', '')
        for device in self.job.device.get('static_info', []):
            if 'probe_channel' in device and 'probe_ip' in device:
                self.probe_channel = device['probe_channel']
                self.probe_ip = device['probe_ip']
                break

    def populate(self, parameters):
        self.internal_pipeline = Pipeline(parent=self, job=self.job, parameters=parameters)
        if self.test_needs_overlay(parameters):
            if any('ssh' in data for data in self.job.device['actions']['deploy']['methods']):
                # only devices supporting ssh deployments add this action.
                self.internal_pipeline.add_action(SshAuthorize())
            self.internal_pipeline.add_action(VlandOverlayAction())
            self.internal_pipeline.add_action(MultinodeOverlayAction())
            self.internal_pipeline.add_action(TestDefinitionAction())
            self.internal_pipeline.add_action(CompressOverlay())
            self.internal_pipeline.add_action(PersistentNFSOverlay())  # idempotent

    def run(self, connection, max_end_time, args=None):  # pylint: disable=too-many-locals
        """
        Check if a lava-test-shell has been requested, implement the overlay
        * create test runner directories beneath the temporary location
        * copy runners into test runner directories
        """
        tmp_dir = self.mkdtemp()
        namespace = self.parameters.get('namespace', None)
        if namespace:
            if namespace not in get_test_action_namespaces(self.job.parameters):
                self.logger.info("[%s] skipped %s - no test action.", namespace, self.name)
                return connection
        self.set_namespace_data(action='test', label='shared', key='location', value=tmp_dir)
        lava_test_results_dir = self.get_namespace_data(action='test', label='results', key='lava_test_results_dir')
        shell = self.get_namespace_data(action='test', label='shared', key='lava_test_sh_cmd')
        self.logger.debug("[%s] Preparing overlay tarball in %s", namespace, tmp_dir)
        lava_path = os.path.abspath("%s/%s" % (tmp_dir, lava_test_results_dir))
        for runner_dir in ['bin', 'tests', 'results']:
            # avoid os.path.join as lava_test_results_dir startswith / so location is *dropped* by join.
            path = os.path.abspath("%s/%s" % (lava_path, runner_dir))
            if not os.path.exists(path):
                os.makedirs(path, 0o755)
                self.logger.debug("makedir: %s", path)
        for fname in self.scripts_to_copy:
            with open(fname, 'r') as fin:
                foutname = os.path.basename(fname)
                output_file = '%s/bin/%s' % (lava_path, foutname)
                if "distro" in fname:
                    distribution = os.path.basename(os.path.dirname(fname))
                    self.logger.debug("Updating %s (%s)", output_file, distribution)
                else:
                    self.logger.debug("Creating %s", output_file)
                with open(output_file, 'w') as fout:
                    fout.write("#!%s\n\n" % shell)
                    if foutname == 'lava-target-mac':
                        fout.write("TARGET_DEVICE_MAC='%s'\n" % self.target_mac)
                    if foutname == 'lava-target-ip':
                        fout.write("TARGET_DEVICE_IP='%s'\n" % self.target_ip)
                    if foutname == 'lava-probe-ip':
                        fout.write("PROBE_DEVICE_IP='%s'\n" % self.probe_ip)
                    if foutname == 'lava-probe-channel':
                        fout.write("PROBE_DEVICE_CHANNEL='%s'\n" % self.probe_channel)
                    if foutname == 'lava-target-storage':
                        fout.write('LAVA_STORAGE="\n')
                        for method in self.job.device.get('storage_info', [{}]):
                            for key, value in method.items():
                                if key == 'yaml_line':
                                    continue
                                self.logger.debug("storage methods:\t%s\t%s", key, value)
                                fout.write(r"\t%s\t%s\n" % (key, value))
                        fout.write('"\n')
                    fout.write(fin.read())
                    os.fchmod(fout.fileno(), self.xmod)

        # Generate the file containing the secrets
        if 'secrets' in self.job.parameters:
            self.logger.debug("Creating %s/secrets", lava_path)
            with open(os.path.join(lava_path, 'secrets'), 'w') as fout:
                for key, value in self.job.parameters['secrets'].items():
                    if key == 'yaml_line':
                        continue
                    fout.write("%s=%s\n" % (key, value))

        connection = super(OverlayAction, self).run(connection, max_end_time, args)
        return connection


class MultinodeOverlayAction(OverlayAction):

    name = "lava-multinode-overlay"
    description = "add lava scripts during deployment for multinode test shell use"
    summary = "overlay the lava multinode scripts"

    def __init__(self):
        super(MultinodeOverlayAction, self).__init__()
        # Multinode-only
        self.lava_multi_node_test_dir = os.path.realpath(
            '%s/../../lava_test_shell/multi_node' % os.path.dirname(__file__))
        self.lava_multi_node_cache_file = '/tmp/lava_multi_node_cache.txt'
        self.role = None
        self.protocol = MultinodeProtocol.name

    def populate(self, parameters):
        # override the populate function of overlay action which provides the
        # lava test directory settings etc.
        pass

    def validate(self):
        super(MultinodeOverlayAction, self).validate()
        # idempotency
        if 'actions' not in self.job.parameters:
            return
        if 'protocols' in self.job.parameters and \
                self.protocol in [protocol.name for protocol in self.job.protocols]:
            if 'target_group' not in self.job.parameters['protocols'][self.protocol]:
                return
            if 'role' not in self.job.parameters['protocols'][self.protocol]:
                self.errors = "multinode job without a specified role"
            else:
                self.role = self.job.parameters['protocols'][self.protocol]['role']

    def run(self, connection, max_end_time, args=None):  # pylint: disable=too-many-locals,too-many-branches,too-many-statements
        if self.role is None:
            self.logger.debug("skipped %s", self.name)
            return connection
        lava_test_results_dir = self.get_namespace_data(action='test', label='results', key='lava_test_results_dir')
        shell = self.get_namespace_data(action='test', label='shared', key='lava_test_sh_cmd')
        location = self.get_namespace_data(action='test', label='shared', key='location')
        if not location:
            raise LAVABug("Missing lava overlay location")
        if not os.path.exists(location):
            raise LAVABug("Unable to find overlay location")

        # the roles list can only be populated after the devices have been assigned
        # therefore, cannot be checked in validate which is executed at submission.
        if 'roles' not in self.job.parameters['protocols'][self.protocol]:
            raise LAVABug("multinode definition without complete list of roles after assignment")

        # Generic scripts
        lava_path = os.path.abspath("%s/%s" % (location, lava_test_results_dir))
        scripts_to_copy = glob.glob(os.path.join(self.lava_multi_node_test_dir, 'lava-*'))
        self.logger.debug(self.lava_multi_node_test_dir)
        self.logger.debug("lava_path: %s", lava_path)
        self.logger.debug("scripts to copy %s", scripts_to_copy)

        for fname in scripts_to_copy:
            with open(fname, 'r') as fin:
                foutname = os.path.basename(fname)
                output_file = '%s/bin/%s' % (lava_path, foutname)
                self.logger.debug("Creating %s", output_file)
                with open(output_file, 'w') as fout:
                    fout.write("#!%s\n\n" % shell)
                    # Target-specific scripts (add ENV to the generic ones)
                    if foutname == 'lava-group':
                        fout.write('LAVA_GROUP="\n')
                        for client_name in self.job.parameters['protocols'][self.protocol]['roles']:
                            if client_name == 'yaml_line':
                                continue
                            role_line = self.job.parameters['protocols'][self.protocol]['roles'][client_name]
                            self.logger.debug("group roles:\t%s\t%s", client_name, role_line)
                            fout.write(r"\t%s\t%s\n" % (client_name, role_line))
                        fout.write('"\n')
                    elif foutname == 'lava-role':
                        fout.write("TARGET_ROLE='%s'\n" % self.job.parameters['protocols'][self.protocol]['role'])
                    elif foutname == 'lava-self':
                        fout.write("LAVA_HOSTNAME='%s'\n" % self.job.job_id)
                    else:
                        fout.write("LAVA_TEST_BIN='%s/bin'\n" % lava_test_results_dir)
                        fout.write("LAVA_MULTI_NODE_CACHE='%s'\n" % self.lava_multi_node_cache_file)
                        # always write out full debug logs
                        fout.write("LAVA_MULTI_NODE_DEBUG='yes'\n")
                    fout.write(fin.read())
                    os.fchmod(fout.fileno(), self.xmod)
        self.call_protocols()
        return connection


class VlandOverlayAction(OverlayAction):
    """
    Adds data for vland interface locations, MAC addresses and vlan names
    """

    name = "lava-vland-overlay"
    description = "Populate specific vland scripts for tests to lookup vlan data."
    summary = "Add files detailing vlan configuration."

    def __init__(self):
        super(VlandOverlayAction, self).__init__()
        # vland-only
        self.lava_vland_test_dir = os.path.realpath(
            '%s/../../lava_test_shell/vland' % os.path.dirname(__file__))
        self.lava_vland_cache_file = '/tmp/lava_vland_cache.txt'
        self.params = {}
        self.sysfs = []
        self.tags = []
        self.names = []
        self.protocol = VlandProtocol.name

    def populate(self, parameters):
        # override the populate function of overlay action which provides the
        # lava test directory settings etc.
        pass

    def validate(self):
        super(VlandOverlayAction, self).validate()
        # idempotency
        if 'actions' not in self.job.parameters:
            return
        if 'protocols' not in self.job.parameters:
            return
        if self.protocol not in [protocol.name for protocol in self.job.protocols]:
            return
        if 'parameters' not in self.job.device:
            self.errors = "Device lacks parameters"
        elif 'interfaces' not in self.job.device['parameters']:
            self.errors = "Device lacks vland interfaces data."
        if not self.valid:
            return
        # same as the parameters of the protocol itself.
        self.params = self.job.parameters['protocols'][self.protocol]
        device_params = self.job.device['parameters']['interfaces']
        vprotocol = [vprotocol for vprotocol in self.job.protocols if vprotocol.name == self.protocol][0]
        # needs to be the configured interface for each vlan.
        for key, _ in self.params.items():
            if key == 'yaml_line' or key not in vprotocol.params:
                continue
            self.names.append(",".join([key, vprotocol.params[key]['iface']]))
        for interface in device_params:
            self.sysfs.append(",".join(
                [
                    interface,
                    device_params[interface]['mac'],
                    device_params[interface]['sysfs'],
                ]))
        for interface in device_params:
            if not device_params[interface]['tags']:
                # skip primary interface
                continue
            for tag in device_params[interface]['tags']:
                self.tags.append(",".join([interface, tag]))

    # pylint: disable=anomalous-backslash-in-string
    def run(self, connection, max_end_time, args=None):
        """
        Writes out file contents from lists, across multiple lines
        VAR="VAL1\n\
        VAL2\n\
        "
        The \n and \ are used to avoid unwanted whitespace, so are escaped.
        \n becomes \\n, \ becomes \\, which itself then needs \n to output:
        VAL1
        VAL2
        """
        if not self.params:
            self.logger.debug("skipped %s", self.name)
            return connection
        location = self.get_namespace_data(action='test', label='shared', key='location')
        lava_test_results_dir = self.get_namespace_data(action='test', label='results', key='lava_test_results_dir')
        shell = self.get_namespace_data(action='test', label='shared', key='lava_test_sh_cmd')
        if not location:
            raise LAVABug("Missing lava overlay location")
        if not os.path.exists(location):
            raise LAVABug("Unable to find overlay location")

        lava_path = os.path.abspath("%s/%s" % (location, lava_test_results_dir))
        scripts_to_copy = glob.glob(os.path.join(self.lava_vland_test_dir, 'lava-*'))
        self.logger.debug(self.lava_vland_test_dir)
        self.logger.debug({"lava_path": lava_path, "scripts": scripts_to_copy})

        for fname in scripts_to_copy:
            with open(fname, 'r') as fin:
                foutname = os.path.basename(fname)
                output_file = '%s/bin/%s' % (lava_path, foutname)
                self.logger.debug("Creating %s", output_file)
                with open(output_file, 'w') as fout:
                    fout.write("#!%s\n\n" % shell)
                    # Target-specific scripts (add ENV to the generic ones)
                    if foutname == 'lava-vland-self':
                        fout.write(r'LAVA_VLAND_SELF="')
                        for line in self.sysfs:
                            fout.write(r"%s\n" % line)
                    elif foutname == 'lava-vland-names':
                        fout.write(r'LAVA_VLAND_NAMES="')
                        for line in self.names:
                            fout.write(r"%s\n" % line)
                    elif foutname == 'lava-vland-tags':
                        fout.write(r'LAVA_VLAND_TAGS="')
                        if not self.tags:
                            fout.write(r"\n")
                        else:
                            for line in self.tags:
                                fout.write(r"%s\n" % line)
                    fout.write('"\n\n')
                    fout.write(fin.read())
                    os.fchmod(fout.fileno(), self.xmod)
        self.call_protocols()
        return connection


class CompressOverlay(Action):
    """
    Makes a tarball of the finished overlay and declares filename of the tarball
    """
    name = "compress-overlay"
    description = "Create a lava overlay tarball and store alongside the job"
    summary = "Compress the lava overlay files"

    def run(self, connection, max_end_time, args=None):
        output = os.path.join(self.job.parameters['output_dir'],
                              "overlay-%s.tar.gz" % self.level)
        location = self.get_namespace_data(action='test', label='shared', key='location')
        lava_test_results_dir = self.get_namespace_data(action='test', label='results', key='lava_test_results_dir')
        self.set_namespace_data(action='test', label='shared', key='output', value=output)
        if not location:
            raise LAVABug("Missing lava overlay location")
        if not os.path.exists(location):
            raise LAVABug("Unable to find overlay location")
        if not self.valid:
            self.logger.error(self.errors)
            return connection
        connection = super(CompressOverlay, self).run(connection, max_end_time, args)
        with chdir(location):
            try:
                with tarfile.open(output, "w:gz") as tar:
                    tar.add(".%s" % lava_test_results_dir)
                    # ssh authorization support
                    if os.path.exists('./root/'):
                        tar.add(".%s" % '/root/')
            except tarfile.TarError as exc:
                raise InfrastructureError("Unable to create lava overlay tarball: %s" % exc)

        self.set_namespace_data(action=self.name, label='output', key='file', value=output)
        return connection


class SshAuthorize(Action):
    """
    Handle including the authorization (ssh public key) into the
    deployment as a file in the overlay and writing to
    /root/.ssh/authorized_keys.
    if /root/.ssh/authorized_keys exists in the test image it will be overwritten
    when the overlay tarball is unpacked onto the test image.
    The key exists in the lava_test_results_dir to allow test writers to work around this
    after logging in via the identity_file set here.
    Hacking sessions already append to the existing file.
    Used by secondary connections only.
    Primary connections need the keys set up by admins.
    """

    name = "ssh-authorize"
    description = 'include public key in overlay and authorize root user'
    summary = 'add public key to authorized_keys'

    def __init__(self):
        super(SshAuthorize, self).__init__()
        self.active = False
        self.identity_file = None

    def validate(self):
        super(SshAuthorize, self).validate()
        if 'to' in self.parameters:
            if self.parameters['to'] == 'ssh':
                return
        if 'authorize' in self.parameters:
            if self.parameters['authorize'] != 'ssh':
                return
        if not any('ssh' in data for data in self.job.device['actions']['deploy']['methods']):
            # idempotency - leave self.identity_file as None
            return
        params = self.job.device['actions']['deploy']['methods']
        check = check_ssh_identity_file(params)
        if check[0]:
            self.errors = check[0]
        elif check[1]:
            self.identity_file = check[1]
        if self.valid:
            self.set_namespace_data(action=self.name, label='authorize', key='identity_file', value=self.identity_file)
            if 'authorize' in self.parameters:
                # only secondary connections set active.
                self.active = True

    def run(self, connection, max_end_time, args=None):
        connection = super(SshAuthorize, self).run(connection, max_end_time, args)
        if not self.identity_file:
            self.logger.debug("No authorisation required.")  # idempotency
            return connection
        # add the authorization keys to the overlay
        location = self.get_namespace_data(action='test', label='shared', key='location')
        lava_test_results_dir = self.get_namespace_data(action='test', label='results', key='lava_test_results_dir')
        if not location:
            raise LAVABug("Missing lava overlay location")
        if not os.path.exists(location):
            raise LAVABug("Unable to find overlay location")
        lava_path = os.path.abspath("%s/%s" % (location, lava_test_results_dir))
        output_file = '%s/%s' % (lava_path, os.path.basename(self.identity_file))
        shutil.copyfile(self.identity_file, output_file)
        shutil.copyfile("%s.pub" % self.identity_file, "%s.pub" % output_file)
        if not self.active:
            # secondary connections only
            return connection
        self.logger.info("Adding SSH authorisation for %s.pub", os.path.basename(output_file))
        user_sshdir = os.path.join(location, 'root', '.ssh')
        if not os.path.exists(user_sshdir):
            os.makedirs(user_sshdir, 0o755)
        # if /root/.ssh/authorized_keys exists in the test image it will be overwritten
        # the key exists in the lava_test_results_dir to allow test writers to work around this
        # after logging in via the identity_file set here
        authorize = os.path.join(user_sshdir, 'authorized_keys')
        self.logger.debug("Copying %s to %s", "%s.pub" % self.identity_file, authorize)
        shutil.copyfile("%s.pub" % self.identity_file, authorize)
        os.chmod(authorize, 0o600)
        return connection


class PersistentNFSOverlay(Action):
    """
    Instead of extracting, just populate the location of the persistent NFS
    so that it can be mounted later when the overlay is applied.
    """

    section = "deploy"
    name = "persistent-nfs-overlay"
    description = "unpack overlay into persistent NFS"
    summary = "add test overlay to NFS"

    def validate(self):
        super(PersistentNFSOverlay, self).validate()
        persist = self.parameters.get('persistent_nfs', None)
        if not persist:
            return
        if 'address' not in persist:
            self.errors = "Missing address for persistent NFS"
            return
        if ':' not in persist['address']:
            self.errors = "Unrecognised NFS URL: '%s'" % self.parameters['persistent_nfs']['address']
            return
        nfs_server, dirname = persist['address'].split(':')
        self.errors = infrastructure_error('rpcinfo')
        self.errors = rpcinfo_nfs(nfs_server)
        self.set_namespace_data(action=self.name, label='nfs_address', key='nfsroot', value=dirname)
        self.set_namespace_data(action=self.name, label='nfs_address', key='serverip', value=nfs_server)