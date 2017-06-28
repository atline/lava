# Copyright (C) 2017 Linaro Limited
#
# Author: Dean Birch <dean.birch@linaro.org>
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

from lava_dispatcher.pipeline.device import NewDevice
from lava_dispatcher.pipeline.parser import JobParser
from lava_dispatcher.pipeline.test.test_basic import Factory, StdoutTestCase
from lava_dispatcher.pipeline.test.utils import DummyLogger


class UefiFactory(Factory):  # pylint: disable=too-few-public-methods

    def create_job(self, filename, output_dir='/tmp'):  # pylint: disable=no-self-use
        device = NewDevice(os.path.join(os.path.dirname(__file__), '../devices/juno-uefi.yaml'))
        y_file = os.path.join(os.path.dirname(__file__), filename)
        with open(y_file) as sample_job_data:
            parser = JobParser()
            job = parser.parse(sample_job_data, device, 4212, None, "",
                               output_dir=output_dir)
        job.logger = DummyLogger()
        return job


class TestUefiShell(StdoutTestCase):

    def setUp(self):
        super(TestUefiShell, self).setUp()
        self.factory = UefiFactory()
        self.job = self.factory.create_job("sample_jobs/juno-uefi-nfs.yaml")

    def test_shell_reference(self):
        self.job.validate()
        self.assertEqual([], self.job.pipeline.errors)
        description_ref = self.pipeline_reference('juno-uefi-nfs.yaml')
        self.assertEqual(description_ref, self.job.pipeline.describe(False))

    def test_device_juno_uefi(self):
        self.assertIsNotNone(self.job)
        self.assertIsNone(self.job.validate())
        self.assertEqual(self.job.device['device_type'], 'juno')

    def test_shell_prompts(self):
        self.job.validate()
        params = self.job.device['actions']['boot']['methods']['uefi']['parameters']
        self.assertIn('shell_interrupt_prompt', params)
        self.assertIn('shell_menu', params)
        self.assertIn('bootloader_prompt', params)
        # Nfs Deploy checks
        deploy = [action for action in self.job.pipeline.actions if action.name == 'nfs-deploy'][0]
        overlay = [action for action in deploy.internal_pipeline.actions if action.name == 'lava-overlay'][0]
        self.assertIsNotNone(overlay)

        # Boot checks
        boot = [action for action in self.job.pipeline.actions if action.name == 'uefi-shell-main-action'][0]
        commands = [action for action in boot.internal_pipeline.actions if action.name == 'bootloader-overlay'][0]
        menu_connect = [action for action in boot.internal_pipeline.actions if action.name == 'menu-connect'][0]
        menu_interrupt = [action for action in boot.internal_pipeline.actions if action.name == 'uefi-shell-menu-interrupt'][0]
        menu_selector = [action for action in boot.internal_pipeline.actions if action.name == 'uefi-shell-menu-selector'][0]
        shell_interrupt = [action for action in boot.internal_pipeline.actions if action.name == 'uefi-shell-menu-interrupt'][0]
        boot_commands = [action for action in boot.internal_pipeline.actions if action.name == 'bootloader-commands'][0]
        self.assertEqual('uefi', commands.method)
        self.assertFalse(commands.use_bootscript)
        self.assertIsNone(commands.lava_mac)
        self.assertIsNotNone(menu_connect)
        self.assertIn('bootloader_prompt', menu_interrupt.params)
        self.assertIn('interrupt_prompt', menu_interrupt.params)
        self.assertIn('boot_message', menu_interrupt.params)
        # First, menu drops to shell...
        self.assertEqual('UEFI Interactive Shell', menu_selector.boot_message)
        # ...then, shell commands boot to linux.
        self.assertEqual('Linux version', boot_commands.params['boot_message'])
        self.assertIsNotNone(shell_interrupt)

    def test_no_menu_reference(self):
        job = self.factory.create_job("sample_jobs/juno-uefi-nfs-no-menu.yaml")
        self.assertEqual([], job.pipeline.errors)
        description_ref = self.pipeline_reference('juno-uefi-nfs-no-menu.yaml', job=job)
        self.assertEqual(description_ref, job.pipeline.describe(False))

    def test_no_menu(self):
        """
        Tests that if shell_menu=='' that the menu is skipped
        """
        job = self.factory.create_job("sample_jobs/juno-uefi-nfs-no-menu.yaml")
        job.validate()
        params = job.device['actions']['boot']['methods']['uefi']['parameters']
        self.assertIn('shell_interrupt_prompt', params)
        self.assertIn('shell_menu', params)
        self.assertIn('bootloader_prompt', params)
        # Nfs Deploy checks
        deploy = [action for action in job.pipeline.actions if action.name == 'nfs-deploy'][0]
        overlay = [action for action in deploy.internal_pipeline.actions if action.name == 'lava-overlay'][0]
        self.assertIsNotNone(overlay)

        # Boot checks
        boot = [action for action in job.pipeline.actions if action.name == 'uefi-shell-main-action'][0]
        commands = [action for action in boot.internal_pipeline.actions if action.name == 'bootloader-overlay'][0]
        boot_commands = [action for action in boot.internal_pipeline.actions if action.name == 'bootloader-commands'][0]

        self.assertIsNotNone([action for action in boot.internal_pipeline.actions if action.name == 'uefi-shell-interrupt'])

        self.assertEquals(0, len([action for action in boot.internal_pipeline.actions if action.name == 'uefi-shell-menu-interrupt']))
        self.assertEquals(0, len([action for action in boot.internal_pipeline.actions if action.name == 'uefi-shell-menu-selector']))

        self.assertEqual('uefi', commands.method)
        self.assertFalse(commands.use_bootscript)
        self.assertIsNone(commands.lava_mac)

        # Shell commands boot to linux.
        self.assertEqual('Linux version', boot_commands.params['boot_message'])
