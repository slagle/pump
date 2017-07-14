#!/usr/bin/env python
#    Licensed under the Apache License, Version 2.0 (the 'License'); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an 'AS IS' BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

import argparse
import json
import logging
import os
import subprocess
import sys
import time
import urlparse

import jinja2
import os_client_config

TIME = time.strftime('%Y-%m-%d-%X')
HEAT_CONFIG_NOTIFY='heat-config-notify'

logger = logging.getLogger()
heat = None

class Server(object):

    def __init__(self, name, id):
        self.name = name
        self.id = id
        self.deployments = {}
        self._resource = None

    def add_deployment(self, deployment, step=0):
        step_deployments = self.deployments.setdefault(step, [])
        step_deployments.append(deployment)

    def stack_url(self):
        stack_url = [l for l in self._resource.links
                     if l['rel'] == 'stack'][0]['href']
        return stack_url

    def stack_path(self):
        stack_path = urlparse.urlparse(self.stack_url()).path
        return stack_path

    def parent_stack_id(self):
        parent_stack_id = self.stack_path().split('/')[-1]
        return parent_stack_id

    @classmethod
    def from_resource(cls, resource):
        server = cls(resource.resource_name, resource.physical_resource_id)
        server._resource = resource
        return server


class Deployment(object):

    def __init__(self, json, step):
        self.json = json
        self.step = step


def configure_logging(args):
    logging.basicConfig()
    logger.setLevel(logging.INFO)
    if args.debug:
        logger.setLevel(logging.DEBUG)

def get_output_dir(parent_dir):
    subdirectory = TIME
    return os.path.join(parent_dir,
                        'pump-output-%s' % subdirectory)

def get_args():
    parser = argparse.ArgumentParser(
                description=("Generate Ansible playbooks from a creating "
                             "Heat stack"),
                formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    parser.add_argument('--stack-name', '-s',
                        default='overcloud',
                        help="Heat stack name")
    parser.add_argument('--nested-depth', '-n',
                        default=10,
                        help=("Nested depth to recurse heat stack for "
                              "resources"))
    parser.add_argument('--server-resource-types',
                        action='append',
                        default=['OS::Nova::Server',
                                 'OS::Heat::DeployedServer'],
                        help=("Heat server resource types to query "
                              "for associated deployments"))
    parser.add_argument('--debug', '-d',
                        action='store_true',
                        default=False,
                        help=('Enable client debugging'))
    parser.add_argument('--output-directory', '-o',
                        help=("Parent output directory for generated output. "
                              "The output will be in timestamped subdirectories "
                              "of the parent directory. Default is "
                              "\"pump-output\" in the current directory"),
                        default=os.path.join(os.getcwd(), 'pump-output'))
    parser.add_argument('--sleep-time',
                        help=("Sleep time in seconds between checking for "
                              "stack complete."),
                        default=30,
                        type=int)
    parser.add_argument('--force', '-f',
                        help=("Force generating all deployments even "
                              "if stack is already CREATE_COMPLETE."),
                        action='store_true',
                        default=False)

    cloud_config = os_client_config.OpenStackConfig()
    cloud_config.register_argparse_arguments(parser, sys.argv)
    args = parser.parse_args(sys.argv[1:])
    return args

def _create_dir(dir):
    if not os.path.exists(dir):
        os.makedirs(dir)

def create_dir_structure(output_dir):
    _create_dir(output_dir)
    _create_dir(os.path.join(output_dir, 'roles'))
    _create_dir(os.path.join(output_dir, 'playbooks'))

def render_deployments(deployments_dir, server_name, server_id, deployments):

    signal_data = {
        'deploy_stdout': '',
        'deploy_stderr': '',
        'deploy_status_code': 0,
    }

    steps = deployments.keys()
    steps.sort()
    for step in steps:
        step_deployments = deployments[step]

        for deployment in step_deployments:
            deployment_file = os.path.join(deployments_dir,
                                           "%s.json" % deployment['id'])
            deployment_notify_file = os.path.join(deployments_dir,
                                           "%s.notify.json" % deployment['id'])

            if not os.path.exists(deployment_file):
                logger.info("writing new deployment %s for server %s" %
                            (deployment['id'], server_name))

                with open(deployment_file, 'w') as f:
                    f.write(json.dumps(deployment))

                with open(deployment_notify_file, 'w') as f_notify:
                    f_notify.write(json.dumps(signal_data))

                command = [HEAT_CONFIG_NOTIFY, deployment_file]
                logger.debug("Running: %s < %s" %
                            (' '.join(command), deployment_notify_file))
                subproc = subprocess.Popen(command,
                                           stdin=subprocess.PIPE,
                                           stdout=subprocess.PIPE,
                                           stderr=subprocess.PIPE)
                stdout, stderr = subproc.communicate(
                                    input=json.dumps(signal_data))
                logger.debug("stdout: %s" % stdout)
                logger.debug("stderr: %s" % stderr)
            else:
                logger.info("deployment %s already written, skipping" %
                             deployment['id'])


def render_ansible(stack_name, roles_dir, servers, step):
    playbook_dir = os.path.join(roles_dir, '..', 'playbooks')
    _create_dir(playbook_dir)
    playbook_file = os.path.join(playbook_dir, '%s.yaml' % stack_name)
    templates_path = os.path.join(
                        os.path.dirname(os.path.realpath(__file__)),
                        'templates')
    env = jinja2.Environment(
            loader=jinja2.FileSystemLoader(templates_path))
    env.trim_blocks = True
    tasks_template = env.get_template('tasks.main.j2.yaml')
    playbook_template = env.get_template('playbook.j2.yaml')

    for server in servers.values():
        server_role_dir = os.path.join(roles_dir, '%s-%s' % (server.name, server.id))
        _create_dir(server_role_dir)
        deployments_dir = os.path.join(server_role_dir, 'deployments')
        _create_dir(deployments_dir)
        tasks_dir = os.path.join(server_role_dir, 'tasks')
        _create_dir(tasks_dir)
        tasks_file = os.path.join(tasks_dir, 'main.yaml')
        role_templates_dir = os.path.join(server_role_dir, 'templates')
        _create_dir(role_templates_dir)
        heat_config_template_file = os.path.join(role_templates_dir, 'heat-config')

        render_deployments(deployments_dir, server.name, server.id,
                           server.deployments)

        with open(tasks_file, 'w') as f:
            f.write(tasks_template.render(server_name=server.name,
                                          steps=server.deployments.keys(),
                                          deployments=server.deployments))

        with open(heat_config_template_file, 'w') as f:
            f.write("[{{ deployment | to_json }}]")


    with open(playbook_file, 'w') as f:
        f.write(playbook_template.render(steps=range(step),
                                         servers=servers))


def stack_complete(stack_name):
    stack_status = heat.stacks.get(stack_name).stack_status
    logger.info("stack %s status: %s" % (stack_name, stack_status))
    return stack_status == 'CREATE_COMPLETE'

def main():
    args = get_args()
    configure_logging(args)
    logger.debug("Debug output enabled...")

    output_dir = get_output_dir(args.output_directory)
    logger.info("output saved in %s" % output_dir)
    create_dir_structure(output_dir)

    global heat
    heat = os_client_config.make_client('orchestration', cloud=args.os_cloud,
                                        debug=args.debug)
    stack = heat.stacks.get(args.stack_name)
    stack_id = stack.id

    servers = {}

    logger.info('starting polling Heat for deployments')

    force = args.force
    if force:
        logger.info("--force specified")

    step = 0
    while not stack_complete(args.stack_name) or force:
        found_servers = []
        for server_resource_type in args.server_resource_types:
            found_servers += heat.resources.list(
                                    stack_id,
                                    nested_depth=args.nested_depth,
                                    filters=dict(type=server_resource_type))

        for found_server in found_servers:
            servers.setdefault(found_server.physical_resource_id,
                               Server.from_resource(found_server))

        for server in servers:
            server_metadata = heat.resources.metadata(
                                    servers[server].parent_stack_id(),
                                    servers[server].name)

            for deployment in server_metadata['deployments']:
                servers[server].add_deployment(deployment, step)

        force = False

        step += 1
        if stack_complete(args.stack_name):
            break

        logger.info("sleeping for %s seconds" % args.sleep_time)
        time.sleep(args.sleep_time)

    logger.info("stack %s is CREATE_COMPLETE, done." % args.stack_name)

    render_ansible(args.stack_name,
                   os.path.join(output_dir, 'roles'),
                   servers,
                   step)

if __name__ == '__main__':
    main()
