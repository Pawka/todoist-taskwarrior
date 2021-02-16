import click
import logging
import os
import sys

from taskw import TaskWarrior
from todoist.api import TodoistAPI
from . import errors, io, utils, validation, gateways
from . import __title__, __version__


# This is the location where the todoist
# data will be cached.
TODOIST_CACHE = '~/.todoist-sync/'

todoist = None
td = None
taskwarrior = None
tw = None


""" CLI Commands """

@click.group()
@click.version_option(version=__version__, prog_name=__title__)
@click.option('--todoist-api-key', envvar='TODOIST_API_KEY', required=True)
@click.option('--tw-config-file', envvar='TASKRC', default='~/.taskrc')
@click.option('--debug', is_flag=True, default=False)
def cli(todoist_api_key, tw_config_file, debug):
    """Manage the migration of data from Todoist into Taskwarrior. """
    global todoist, taskwarrior, td, tw

    # Configure Todoist with API key and cache
    todoist = TodoistAPI(todoist_api_key, cache=TODOIST_CACHE)
    td = gateways.Todoist(todoist_api_key)

    # Create the TaskWarrior client, overriding config with `todoist_id` field
    # which we will use to track migrated tasks and prevent imports.
    # The path to the taskwarrior config file can be set with the flag, but
    # otherwise, the TASKRC envvar will be used if present. The taskwarrior
    # default value is used if neither are specified.
    taskwarrior = TaskWarrior(
        config_filename=tw_config_file,
        config_overrides={ 'uda.todoist_id.type': 'string' },
    )
    tw = gateways.TaskWarrior(tw_config_file)

    # Setup logging
    level = logging.DEBUG if debug else logging.INFO
    logging.basicConfig(level=level)



@cli.command()
def synchronize():
    """Update the local Todoist task cache.

    This command accesses Todoist via the API and updates a local
    cache before exiting. This can be useful to pre-load the tasks,
    and means `migrate` can be run without a network connection.

    NOTE - the local Todoist data cache is usually located at:

        ~/.todoist-sync
    """
    with io.with_feedback('Syncing tasks with todoist'):
        td.sync()


@cli.command()
@click.confirmation_option(prompt=f'Are you sure you want to delete {TODOIST_CACHE}?')
def clean():
    """Remove the data stored in the Todoist task cache.

    NOTE - the local Todoist data cache is usually located at:

        ~/.todoist-sync
    """
    cache_dir = os.path.expanduser(TODOIST_CACHE)

    # Delete all files in directory
    for file_entry in os.scandir(cache_dir):
        with io.with_feedback(f'Removing file {file_entry.path}'):
            os.remove(file_entry)

    # Delete directory
    with io.with_feedback(f'Removing directory {cache_dir}'):
        os.rmdir(cache_dir)


@cli.command()
@click.option('--sync/--no-sync', default=True,
        help='Enable/disable Todoist synchronization of the local task cache.')
@click.option('-p', '--map-project', metavar='SRC=DST', multiple=True,
        callback=validation.validate_map,
        help='Project names specified will be translated from SRC to DST. '
             'If DST is omitted, the project will be unset when SRC matches.')
@click.option('-t', '--map-tag', metavar='SRC=DST', multiple=True,
        callback=validation.validate_map,
        help='Tags specified will be translated from SRC to DST. '
             'If DST is omitted, the tag will be removed when SRC matches.')
@click.option('--filter-task-id', type=int,
        help='Only import a task matching the given ID')
@click.option('--filter-proj-id', type=int,
        help='Only import the tasks in the project matching the given ID')
@click.pass_context
def migrate(ctx, sync, map_project, map_tag, filter_task_id, filter_proj_id):
    """Migrate tasks from Todoist to Taskwarrior.

    By default this command will synchronize with the Todoist servers
    and then migrate all tasks to Taskwarrior.

    Pass --no-sync to skip synchronization.

    Use --map-project to change or remove the project. Project hierarchies will
    be period-delimited during conversion. For example in the following,
    'Work Errands' and 'House Errands' will be both be changed to 'errands',
    'Programming.Open Source' will be changed to 'oss', and the project will be
    removed when it is 'Taxes':
    \r
    --map-project 'Work Errands'=errands
    --map-project 'House Errands'=errands
    --map-project 'Programming.Open Source'=oss
    --map-project Taxes=

    This command can be run multiple times and will not duplicate tasks.
    This is tracked in Taskwarrior by setting and detecting the
    `todoist_id` property on the task.
    """
    logging.debug(
        f'MIGRATE version={__version__} '
        f'sync={sync} map_project={map_project} map_tag={map_tag} '
        f'filter_task_id={filter_task_id} filter_proj_id={filter_proj_id}'
    )

    if sync:
        ctx.invoke(synchronize)

    # Get all matching Todoist tasks
    tasks = td.get_tasks(filter_task_id, filter_proj_id)
    if not tasks:
        io.warn('No matching tasks found (are you using filters?)')
        return

    io.important(f'Starting migration of {len(tasks)} tasks...')
    for idx, task in enumerate(tasks):
        tid = task['id']
        name = task['content']

        # Log message and check if exists
        io.important(f'Task {idx + 1} of {len(tasks)}: {name}')
        logging.debug(f'ITER_TASK task={task}')
        tw_task = tw.get_task(tid)
        data = map_to_tw(task, map_project, map_tag)
        if tw_task:
            io.info(f'Already exists (todoist_id={tid})')
            if close_if_needed(tw_task, task):
                io.info(f'Closed task (todoist_id={tid})')
                continue

            if tw_task['status'] == TW_STATUS_PENDING:
                tw.update(tw_task, data)
                io.info(f'Updated task (todoist_id={tid})')
            continue

        tw_task = tw.add_task(**data)
        if tw_task:
            if close_if_needed(tw_task, task):
                io.info(f'Closed task (todoist_id={tid})')


def map_to_tw(task, map_project, map_tag):
    """Map Todoist task to TaskWarrior task."""
    project_name = td.project_name_from_todoist(task['project_id'], map_project)
    data = {
        'tid': task['id'],
        'description': task['content'],
        'project': utils.maybe_quote_ws(project_name),
        'priority': utils.parse_priority(task['priority']),
        'entry': utils.parse_date(task['date_added']),
        'due': utils.parse_due(utils.try_get_model_prop(task, 'due')),
        'recur': parse_recur_or_prompt(utils.try_get_model_prop(task, 'due')),
    }

    # Tags
    logging.debug(f"TAGS labels={task['labels']}")
    data['tags'] = [
        utils.try_map(map_tag, todoist.labels.get_by_id(l_id)['name'])
        for l_id in task['labels']
    ]

    # Dates
    return data


@cli.command()
@click.option('--sync/--no-sync', default=True,
        help='Enable/disable Todoist synchronization of the local task cache.')
@click.option('--taskw/--no-taskw', default=True,
              help='Enable/disable synchronization to TaskWarrior.')
@click.option('--todoist/--no-todoist', default=True,
              help='Enable/disable synchronization to Todoist.')
@click.pass_context
def sync(ctx, sync, taskw, todoist):
    """2-way synchronization between TaskWarrior and Todoist.
    """
    # TODO: bad naming of option. Could be --todoist-cache.
    if sync:
        ctx.invoke(synchronize)

    todoist_tasks = td.get_tasks()

    if todoist:
        close_todoist_tasks(todoist_tasks)

    if taskw is True:
        ctx.invoke(migrate)


TW_STATUS_PENDING = "pending"
TW_STATUS_COMPLETED = "completed"


def close_todoist_tasks(tdtasks):
    """Close tasks on Todoist if those are already closed on TaskWarrior."""
    for task in tdtasks:
        tid = task['id']
        twtask = tw.get_task(tid)
        if (twtask and twtask["status"] == TW_STATUS_COMPLETED
                and task['checked'] != 1):
            io.info(f'Closed Todoist task (todoist_id={tid})')
            todoist.items.close(task['id'])
    todoist.commit()
    todoist.sync()


def close_if_needed(tw_task, task):
    """Close existing TaskWarrior task if it is closed on Todoist.

    True is returned if task is closed.

    TODO: And vice versa later when sync from TW to Todoist will be available.
    """
    if task['checked'] == 1 and tw_task['status'] == TW_STATUS_PENDING:
        taskwarrior.task_done(id=tw_task['id'])
        return True
    return False


def parse_recur_or_prompt(due):
    try:
        return utils.parse_recur(due)
    except errors.UnsupportedRecurrence:
        io.error("Unsupported recurrence: '%s'. Please enter a valid value" % due['string'])
        return io.prompt(
            'Set recurrence (todoist style)',
            default='',
            value_proc=validation.validate_recur,
        )


""" Entrypoint """

if __name__ == '__main__':
    cli()
