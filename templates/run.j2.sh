#!/bin/bash

set -eux

ansible-playbook \
    -i inventory \
    playbooks/{{ stack_name }}.yaml \
    $@

