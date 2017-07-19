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
STACK_CACHE = {}
STACK_RESOURCE_CACHE = {}

logger = logging.getLogger('pump')
heat = None

class Server(object):

    def __init__(self, name, id, parent_resource):
        if name == "deployed-server":
            self.role_name = parent_resource
        else:
            self.role_name = name

        self.name = name
        self.id = id
        self.deployments = {}
        self.deployment_ids = []
        self.parent_resource = parent_resource
        self._resource = None
        self.__resource_path = None
        self.__stack_url = None
        self.__stack_path = None
        self.__parent_stack_id = None
        self.__resource_path = None
        self.__heat_name = None

    def add_deployment(self, deployment, step=0):
        if deployment['id'] not in self.deployment_ids:
            step_deployments = self.deployments.setdefault(step, [])
            step_deployments.append(deployment)
            self.deployment_ids.append(deployment['id'])
            sorted_deployments = sorted(self.deployments[step],
                                        key=lambda d: d['creation_time'])
            self.deployments[step] = sorted_deployments

    @property
    def heat_name(self):
        if self.__heat_name is None:
            self.__heat_name = heat.resources.get(self.parent_stack_id,
                                                  self.name).attributes['name']
        return self.__heat_name

    @property
    def stack_url(self):
        if self.__stack_url is None:
            self.__stack_url = stack_url = [l for l in self._resource.links
                         if l['rel'] == 'stack'][0]['href']
        return self.__stack_url

    @property
    def stack_path(self):
        if self.__stack_path is None:
            self.__stack_path = urlparse.urlparse(self.stack_url).path
        return self.__stack_path

    @property
    def parent_stack_name(self):
        if self.__parent_stack_name is None:
            self.__parent_stack_name = self.stack_path.split('/')[-2]
        return self.__parent_stack_name

    @property
    def parent_stack_id(self):
        if self.__parent_stack_id is None:
            self.__parent_stack_id = self.stack_path.split('/')[-1]
        return self.__parent_stack_id

    def unique_name(self):
        parent_name = self.parent_resource
        if parent_name is None:
            parent_name = self.parent_stack_name
        return "%s-%s" % (self.name, self.id)

    @property
    def resource_path(self):
        if not self.__resource_path:
            paths = get_resource_path(self.parent_stack_id, self.id)
            self.__resource_path = paths[0:-1]
            self.__resource_path.append(self.heat_name)

        return self.__resource_path

    @classmethod
    def from_resource(cls, resource):
        parent_resource = getattr(resource, 'parent_resource', None)
        server = cls(resource.resource_name, resource.physical_resource_id,
                     parent_resource)
        server._resource = resource
        return server


def get_stack(stack_id):
    if stack_id in STACK_CACHE:
        logger.debug("STACK_CACHE hit for %s" % stack_id)
        return STACK_CACHE[stack_id]

    stack = heat.stacks.get(stack_id)
    if 'COMPLETE' in stack.stack_status:
        STACK_CACHE[stack_id] = stack

    return stack

def get_stack_resources(stack_id):
    if stack_id in STACK_CACHE and stack_id in STACK_RESOURCE_CACHE:
        logger.debug("STACK_RESOURCE_CACHE hit for %s" % stack_id)
        return STACK_RESOURCE_CACHE[stack_id]

    resources = heat.resources.list(stack_id)
    STACK_RESOURCE_CACHE[stack_id] = resources
    return resources

def get_resource_path(parent_stack_id, resource_id):
    """Creates path to a given resource by name from the top level stack all
    the way to the resource
    """

    def get_resource(parent_stack_id, resource_id):
        # need to loop here as sometimes no resources are found
        # in the parent stack that match the resource id
        # not sure why, perhaps it's a race between pump and Heat
        while True:
            parent_stack_resources = get_stack_resources(parent_stack_id)
            resources = [r for r in parent_stack_resources
                            if r.physical_resource_id == resource_id]
            if resources:
                return resources[0]
            logger.info("in get_resource loop still")


    def get_path(parent_stack_id, resource_id):
        parent_stack = get_stack(parent_stack_id)
        resource = get_resource(parent_stack_id, resource_id)
        paths.insert(0, resource.resource_name)

        if parent_stack.parent:
            get_path(parent_stack.parent, parent_stack_id)

    paths = []
    get_path(parent_stack_id, resource_id)
    return paths

def configure_logging(args):
    logging.basicConfig()
    logger.setLevel(logging.INFO)
    if args.debug:
        logger.setLevel(logging.DEBUG)

def get_output_dir(parent_dir, stack_name):
    subdirectory = TIME
    return os.path.join(parent_dir,
                        'pump-output-%s-%s' % (stack_name, subdirectory))

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
    parser.add_argument('--no-signal',
                        help=("Do not signal deployments. Useful with "
                              "--force"),
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

def render_deployments(server,
                       output_dir,
                       step,
                       no_signal=False):
    logger.info('render_deployments for %s step %s' %
                    (server.heat_name, step))

    signal_data = {
        'deploy_stdout': '',
        'deploy_stderr': '',
        'deploy_status_code': 0,
    }

    roles_dir = os.path.join(output_dir, 'roles')
    role_dir = os.path.join(roles_dir, server.role_name)
    _create_dir(role_dir)
    server_tasks_dir = os.path.join(role_dir, 'tasks', server.heat_name)
    _create_dir(server_tasks_dir)

    server_name = server.heat_name
    server_id = server.id
    deployments = server.deployments

    step_deployments = deployments.get(step, [])

    for deployment in step_deployments:

        if not deployment.get('resource_path'):
            deploy_stack_id = [d for d in deployment['inputs']
                                if d['name'] == 'deploy_stack_id'][0]['value']
            deploy_resource_name = [d for d in deployment['inputs']
                                     if d['name'] == 'deploy_resource_name'][0]['value']
            deployment_id = heat.resources.get(deploy_stack_id, deploy_resource_name).physical_resource_id
            deployment['deployment_id'] = deployment_id
            deploy_stack = get_stack(deploy_stack_id).id
            resource_path = os.path.join(*get_resource_path(
                                               deploy_stack,
                                               deployment_id))

            head, tail = os.path.split(resource_path)
            try:
                int(tail)
                resource_path = head
            except ValueError:
                pass

            deployment['resource_path'] = resource_path


        deployments_file = os.path.join(server_tasks_dir, 'deployments',
                                        deployment['resource_path'])

        _create_dir(os.path.dirname(deployments_file))

        deployment_file = '%s.json' % deployments_file
        deployment_notify_file = '%s.notify.json' % deployments_file

        logger.info("  writing new deployment %s for server %s" %
                    (deployment['id'], server_name))

        with open(deployment_file, 'w') as f:
            f.write(json.dumps(deployment, sort_keys=True, indent=2))

        with open(deployment_notify_file, 'w') as f_notify:
            f_notify.write(json.dumps(signal_data))

        if not no_signal:
            command = [HEAT_CONFIG_NOTIFY, deployment_file]
            logger.info("  Signaling deployment %s" % deployment['id'])
            logger.debug("    Running: %s < %s" %
                        (' '.join(command), deployment_notify_file))
            subproc = subprocess.Popen(command,
                                       stdin=subprocess.PIPE,
                                       stdout=subprocess.PIPE,
                                       stderr=subprocess.PIPE)
            stdout, stderr = subproc.communicate(
                                input=json.dumps(signal_data))
            logger.debug("stdout: %s" % stdout)
            logger.debug("stderr: %s" % stderr)


def render_ansible(stack_name, roles_dir, servers, step, no_signal):
    logger.debug('render_ansible')

    playbook_dir = os.path.join(roles_dir, '..', 'playbooks')
    _create_dir(playbook_dir)
    playbook_file = os.path.join(playbook_dir, '%s.yaml' % stack_name)
    templates_path = os.path.join(
                        os.path.dirname(os.path.realpath(__file__)),
                        'templates')

    env = jinja2.Environment(
            loader=jinja2.FileSystemLoader(templates_path))
    env.trim_blocks = True
    step_tasks_template = env.get_template('tasks.server.steps.j2.yaml')
    main_tasks_template = env.get_template('tasks.main.j2.yaml')
    playbook_template = env.get_template('playbook.j2.yaml')
    run_template = env.get_template('run.j2.sh')
    cfg_template = env.get_template('ansible.cfg.j2')
    defaults_main_template = env.get_template('defaults.main.j2.yaml')


    roles = {}

    for server in servers.values():

        role = server.role_name
        roles.setdefault(role, []).append(server)

        role_dir = os.path.join(roles_dir, role)
        _create_dir(role_dir)

        server_tasks_dir = os.path.join(role_dir, 'tasks', server.heat_name)
        _create_dir(server_tasks_dir)


        role_templates_dir = os.path.join(role_dir, 'templates')
        _create_dir(role_templates_dir)
        heat_config_template_file = os.path.join(role_templates_dir, 'heat-config')

        for s in range(step+1):
            step_deployments = server.deployments.get(s)
            if step_deployments:
                step_file = os.path.join(server_tasks_dir, "step%s.yaml" % s)
                with open(step_file, 'w') as f:
                    f.write(step_tasks_template.render(deployments=step_deployments,
                                                       server_name=server.heat_name,
                                                       step=s))

    with open(heat_config_template_file, 'w') as f:
        f.write("[{{ deployment | to_json }}]")

    with open(playbook_file, 'w') as f:
        logger.info("rendering %s" % playbook_file)
        f.write(playbook_template.render(steps=range(step+1),
                                         servers=servers,
                                         roles=roles))

    tasks_dir = os.path.join(role_dir, 'tasks')
    _create_dir(tasks_dir)
    main_tasks_file = os.path.join(tasks_dir, 'main.yaml')
    with open(main_tasks_file, 'w') as f:
        logger.info("rendering %s" % main_tasks_file)
        f.write(main_tasks_template.render())

    run_dir = os.path.join(roles_dir, '..')
    _create_dir(run_dir)
    run_file = os.path.join(run_dir, 'run.sh')
    with open(run_file, 'w') as f:
        logger.info("rendering %s" % run_file)
        f.write(run_template.render(stack_name=stack_name))
        os.chmod(run_file, 0755)

    cfg_file = os.path.join(run_dir, 'ansible.cfg')
    with open(cfg_file, 'w') as f:
        logger.info("rendering %s" % "ansible.cfg")
        f.write(cfg_template.render(stack_name=stack_name))

    log_file = os.path.join(roles_dir, '..', '%s.log' % stack_name)
    if not os.path.exists(log_file):
        os.mknod(log_file)

    defaults_dir = os.path.join(role_dir, 'defaults')
    _create_dir(defaults_dir)
    defaults_file = os.path.join(defaults_dir, 'main.yaml')
    with open(defaults_file, 'w') as f:
        logger.info("rendering %s" % defaults_file)
        f.write(defaults_main_template.render())

def stack_complete(stack_name):
    stack_status = get_stack(stack_name).stack_status
    logger.info("stack %s status: %s" % (stack_name, stack_status))

    if 'FAILED' in stack_status:
        raise Exception("stack %s FAILED" % stack_name)

    return stack_status in ('CREATE_COMPLETE', 'UPDATE_COMPLETE')

def main():
    args = get_args()
    configure_logging(args)
    logger.debug("Debug output enabled...")

    output_dir = get_output_dir(args.output_directory, args.stack_name)
    logger.info("output saved in %s" % output_dir)
    create_dir_structure(output_dir)

    global heat
    heat = os_client_config.make_client('orchestration', cloud=args.os_cloud,
                                        debug=args.debug)
    stack = get_stack(args.stack_name)
    stack_id = stack.id

    servers = {}

    logger.info('starting polling Heat for deployments')

    force = args.force
    if force:
        logger.info("--force specified")

    step = -1
    # Loop until the stack is complete, or if force was specified, in which
    # case the stack is already complete, so we will loop once
    while not stack_complete(args.stack_name) or force:
        step += 1
        found_servers = []
        # Finds all resources in the stack that are of type
        # args.server_resource_types
        for server_resource_type in args.server_resource_types:
            found_servers += heat.resources.list(
                                    stack_id,
                                    nested_depth=args.nested_depth,
                                    filters=dict(type=server_resource_type))

        logger.debug("found servers: %s" %
                     [s.resource_name for s in found_servers])
        for found_server in found_servers:
            servers.setdefault(found_server.physical_resource_id,
                               Server.from_resource(found_server))

        # Pull deployment metadata for each server, this will get all
        # deployments, regardless if they are complete or not
        for server in servers:
            server_metadata = heat.resources.metadata(
                                    servers[server].parent_stack_id,
                                    servers[server].name)

            for deployment in server_metadata['deployments']:
                servers[server].add_deployment(deployment, step)

            render_deployments(servers[server], output_dir, step, args.no_signal)

        # Set force to False to break the "while" loop
        force = False

        if stack_complete(args.stack_name):
            break

        logger.info("sleeping for %s seconds" % args.sleep_time)
        time.sleep(args.sleep_time)

    # Create ansible files.
    render_ansible(args.stack_name,
                   os.path.join(output_dir, 'roles'),
                   servers,
                   step,
                   args.no_signal)


    logger.info("stack %s is CREATE_COMPLETE, done." % args.stack_name)


if __name__ == '__main__':
    main()
