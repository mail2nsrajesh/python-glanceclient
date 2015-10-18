#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

import re

from tempest_lib import exceptions

from glanceclient.tests.functional import base


class SimpleReadOnlyGlanceClientTest(base.ClientTestBase):

    """Read only functional python-glanceclient tests.

    This only exercises client commands that are read only.
    """

    def test_list_v1(self):
        out = self.glance('--os-image-api-version 1 image-list')
        endpoints = self.parser.listing(out)
        self.assertTableStruct(endpoints, [
            'ID', 'Name', 'Disk Format', 'Container Format',
            'Size', 'Status'])

    def test_list_v2(self):
        out = self.glance('--os-image-api-version 2 image-list')
        endpoints = self.parser.listing(out)
        self.assertTableStruct(endpoints, ['ID', 'Name'])

    def test_fake_action(self):
        self.assertRaises(exceptions.CommandFailed,
                          self.glance,
                          'this-does-not-exist')

    def test_member_list_v1(self):
        tenant_name = '--tenant-id %s' % self.creds['project_name']
        out = self.glance('--os-image-api-version 1 member-list',
                          params=tenant_name)
        endpoints = self.parser.listing(out)
        self.assertTableStruct(endpoints,
                               ['Image ID', 'Member ID', 'Can Share'])

    def test_member_list_v2(self):
        try:
            # NOTE(flwang): If set disk-format and container-format, Jenkins
            # will raise an error said can't recognize the params, thouhg it
            # works fine at local. Without the two params, Glance will
            # complain. So we just catch the exception can skip it.
            self.glance('--os-image-api-version 2 image-create --name temp')
        except Exception:
            pass
        out = self.glance('--os-image-api-version 2 image-list'
                          ' --visibility private')
        image_list = self.parser.listing(out)
        # NOTE(flwang): Because the member-list command of v2 is using
        # image-id as required parameter, so we have to get a valid image id
        # based on current environment. If there is no valid image id, we will
        # pass in a fake one and expect a 404 error.
        if len(image_list) > 0:
            param_image_id = '--image-id %s' % image_list[0]['ID']
            out = self.glance('--os-image-api-version 2 member-list',
                              params=param_image_id)
            endpoints = self.parser.listing(out)
            self.assertTableStruct(endpoints,
                                   ['Image ID', 'Member ID', 'Status'])
        else:
            param_image_id = '--image-id fake_image_id'
            self.assertRaises(exceptions.CommandFailed,
                              self.glance,
                              '--os-image-api-version 2 member-list',
                              params=param_image_id)

    def test_help(self):
        help_text = self.glance('--os-image-api-version 2 help')
        lines = help_text.split('\n')
        self.assertFirstLineStartsWith(lines, 'usage: glance')

        commands = []
        cmds_start = lines.index('Positional arguments:')
        cmds_end = lines.index('Optional arguments:')
        command_pattern = re.compile('^ {4}([a-z0-9\-\_]+)')
        for line in lines[cmds_start:cmds_end]:
            match = command_pattern.match(line)
            if match:
                commands.append(match.group(1))
        commands = set(commands)
        wanted_commands = set(('image-create', 'image-delete', 'help',
                               'image-download', 'image-show', 'image-update',
                               'member-create', 'member-delete',
                               'member-list', 'image-list'))
        self.assertFalse(wanted_commands - commands)

    def test_version(self):
        self.glance('', flags='--version')

    def test_debug_list(self):
        self.glance('--os-image-api-version 2 image-list', flags='--debug')

    def test_no_ssl_compression(self):
        # Test deprecating this hasn't broken anything
        out = self.glance('--os-image-api-version 1 '
                          '--no-ssl-compression image-list')
        endpoints = self.parser.listing(out)
        self.assertTableStruct(endpoints, [
            'ID', 'Name', 'Disk Format', 'Container Format',
            'Size', 'Status'])
