"""Operator tools for reviewable task-specific custody grants."""
import json
from pathlib import Path

from .compute import delegation
from .exchange import Envelope


def add_parser(commands):
    root = commands.add_parser('delegate', help='dataset custody and delegated execution')
    subs = root.add_subparsers(dest='delegate_command', required=True)
    subs.add_parser('catalog')
    prepare = subs.add_parser('prepare')
    prepare.add_argument('--requester', required=True)
    prepare.add_argument('--executor', required=True)
    prepare.add_argument('--site', required=True)
    prepare.add_argument('--dataset', action='append', required=True)
    prepare.add_argument('--workflow', choices=sorted(delegation.COHORT_WORKFLOWS), required=True)
    prepare.add_argument('--region', required=True)
    prepare.add_argument('--out', type=Path, required=True)
    approve = subs.add_parser('approve', help='sign a reviewed exact task as this dataset custodian')
    approve.add_argument('task', type=Path)
    approve.add_argument('--minutes', type=int, default=15)
    approve.add_argument('--out', type=Path, required=True)
    for action in ('run', 'send'):
        run = subs.add_parser(action)
        run.add_argument('task', type=Path)
        run.add_argument('--grant', type=Path, action='append', default=[])


def command(args, town, log):
    action = args.delegate_command
    if action == 'catalog':
        result = delegation.catalog(town)
    elif action == 'prepare':
        result = delegation.prepare(town, requester=args.requester, executor=args.executor, site=args.site,
                                    datasets=args.dataset, workflow=args.workflow, region=args.region)
        args.out.write_text(json.dumps(result, indent=2) + '\n')
        delegation.record_request(town, result, log)
    else:
        task = json.loads(args.task.read_text())
        if action == 'approve':
            result = delegation.approve(town, task, minutes=args.minutes)
            args.out.write_text(json.dumps(result, indent=2) + '\n')
            log.event(town.name, 'delegation_approved', task['id'], {'task_id': task['id'], 'custodian': town.name,
                      'executor': task['executor'], 'datasets': result['datasets'], 'expires': result['valid_until']})
        else:
            grants = [json.loads(path.read_text()) for path in args.grant]
            if action == 'run':
                delegation.record_request(town, task, log)
                result = delegation.execute(town, task, grants, log=log)
            else:
                from .peers import _send_federated
                if task.get("requester") != town.name:
                    raise delegation.ComputeError("send must use the approved requester town identity")
                delegation.inspect_task(town, task)
                envelope = Envelope.new('question', town.name, task['executor'],
                                        {'operation': 'delegated-compute', 'task': task, 'grants': grants})
                result = {'id': envelope.id, **_send_federated(town, envelope, log)}
                log.event(town.name, 'delegation_sent', envelope.id,
                          {'task_id': task['id'], 'requester': task['requester'], 'executor': task['executor']})
    print(json.dumps(result, indent=2))
    return 0
