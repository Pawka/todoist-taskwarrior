import click
import logging
import os
import sys

from taskw import TaskWarrior
from todoist.api import TodoistAPI
from . import errors, io, utils, validation
from . import __title__, __version__


# This is the location where the todoist
# data will be cached.
TODOIST_CACHE = '~/.todoist-sync/'

todoist = None
taskwarrior = None


""" CLI Commands """

@click.group()
@click.version_option(version=__version__, prog_name=__title__)
@click.option('--todoist-api-key', envvar='TODOIST_API_KEY', required=True)
@click.option('--tw-config-file', envvar='TASKRC', default='~/.taskrc')
@click.option('--debug', is_flag=True, default=False)
def cli(todoist_api_key, tw_config_file, debug):
    """Manage the migration of data from Todoist into Taskwarrior. """
    global todoist, taskwarrior

    # Configure Todoist with API key and cache
    todoist = TodoistAPI(todoist_api_key, cache=TODOIST_CACHE)

    # Create the TaskWarrior client, overriding config with `todoist_id` field
    # which we will use to track migrated tasks and prevent imports.
    # The path to the taskwarrior config file can be set with the flag, but
    # otherwise, the TASKRC envvar will be used if present. The taskwarrior
    # default value is used if neither are specified.
    taskwarrior = TaskWarrior(
        config_filename=tw_config_file,
        config_overrides={ 'uda.todoist_id.type': 'string' },
    )

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
        todoist.sync()


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
@click.option('-i', '--interactive', is_flag=True, default=False,
        help='Interactively choose which tasks to import and modify them '
             'during the import.')
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
def migrate(ctx, interactive, sync, map_project, map_tag, filter_task_id, filter_proj_id):
    """Migrate tasks from Todoist to Taskwarrior.

    By default this command will synchronize with the Todoist servers
    and then migrate all tasks to Taskwarrior.

    Pass --no-sync to skip synchronization.

    Passing -i or --interactive allows more control over the import, putting
    the user into an interactive command loop. Per task, the user can decide
    whether to skip, rename, change the priority, or change the tags, before
    moving on to the next task.

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
        f'MIGRATE version={__version__} interactive={interactive} '
        f'sync={sync} map_project={map_project} map_tag={map_tag} '
        f'filter_task_id={filter_task_id} filter_proj_id={filter_proj_id}'
    )

    if sync:
        ctx.invoke(synchronize)

    # Get all matching Todoist tasks
    tasks = get_todoist_tasks(filter_task_id, filter_proj_id)
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
        tw_task = get_task(tid)
        if tw_task:
            io.info(f'Already exists (todoist_id={tid})')
            if close_if_needed(tw_task, task):
                io.info(f'Closed task (todoist_id={tid})')
            continue

        data = map_to_tw(task)
        if not interactive:
            add_task(**data)
        else:
            add_task_interactive(**data)


def project_name_from_todoist(project_id, map_project):
    # Project
    p = todoist.projects.get_by_id(project_id)
    logging.debug(f"GET_PROJECT_BY_ID project_id={project_id} project={p}")
    project_name = ''
    if p:
        project_hierarchy = [p]
        while p['parent_id']:
            p = todoist.projects.get_by_id(p['parent_id'])
            project_hierarchy.insert(0, p)
            logging.debug(f"PROJECT_HIERARCHY parent_id={p['parent_id']} hierarchy={project_hierarchy}")
        project_name = '.'.join(p['name'] for p in project_hierarchy)
        logging.debug(f'PROJECT_HIERARCHY project_name={project_name}')

        project_name = utils.try_map(
            map_project,
            project_name
        )
    return project_name


def map_to_tw(task, map_project, map_tag):
    """Map Todoist task to TaskWarrior task."""
    project_name = project_name_from_todoist(task['project_id'], map_project)
    data = {
        'tid': task['id'],
        'name': task['content'],
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

    todoist_tasks = get_todoist_tasks()

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
        twtask = get_task(tid)
        if (twtask and twtask["status"] == TW_STATUS_COMPLETED
                and task['checked'] != 1):
            io.info(f'Closed Todoist task (todoist_id={tid})')
            todoist.items.close(task['id'])
    todoist.commit()
    todoist.sync()


def get_pending_tasks():
    """Return pending TaskWarrior tasks.

    This does not include tasks which are waiting to be displayed (pending but
    not displayed in TaskWarrior because current date hasn't reached the
    "Waiting" date yet).
    """
    return taskwarrior.filter_tasks({"status": TW_STATUS_PENDING})


def get_task(tid):
    """ Given a Todoist ID, check if the task exists """
    _, task = taskwarrior.get_task(todoist_id=tid)
    return task


def close_if_needed(tw_task, task):
    """Close existing TaskWarrior task if it is closed on Todoist.

    True is returned if task is closed.

    TODO: And vice versa later when sync from TW to Todoist will be available.
    """
    if task['checked'] == 1 and tw_task['status'] == TW_STATUS_PENDING:
        taskwarrior.task_done(id=tw_task['id'])
        return True
    return False


def add_task(tid, name, project, tags, priority, entry, due, recur):
    """Add a taskwarrior task from todoist task

    Returns the taskwarrior task.
    """
    with io.with_feedback(f"Importing '{name}' ({project})"):
        return taskwarrior.task_add(
            name,
            project=project,
            tags=tags,
            priority=priority,
            entry=entry,
            due=due,
            recur=recur,
            todoist_id=tid,
        )


def add_task_interactive(**task_data):
    """Interactively add tasks

    y - add task
    n - skip task
    d - change description
    P - change project
    p - change priority
    t - change tags
    r - change recur
    q - quit immediately
    ? - print help
    """
    callbacks = {
        'y': lambda: task_data,
        'n': lambda: task_data,

        # Rename
        'd': lambda: {
            **task_data,
            'name': io.prompt(
                'Set name',
                default=task_data['name'],
                value_proc=lambda x: x.strip(),
            ),
        },

        # Edit tags
        't': lambda: {
            **task_data,
            'tags': io.prompt(
                'Set tags (space delimited)',
                default=' '.join(task_data['tags']),
                show_default=False,
                value_proc=lambda x: x.split(' '),
            ),
        },

        # Edit project
        'P': lambda: {
            **task_data,
            'project': io.prompt(
                'Set project',
                default=task_data['project'],
            ),
        },

        # Edit priority
        'p': lambda: {
            **task_data,
            'priority': io.prompt(
                'Set priority',
                default='',
                show_default=False,
                type=click.Choice(['L', 'M', 'H', '']),
            ),
        },

        # Edit recur
        'r': lambda: {
            **task_data,
            'recur': io.prompt(
                'Set recurrence (todoist style)',
                default='',
                value_proc=validation.validate_recur,
            ),
        },


        # Quit
        'q': lambda: exit(1),

        # Help message
        # Note: this echoes prompt help and then returns the
        # task_data unchanged.
        '?': lambda: io.warn('\n'.join([
            x.strip() for x in
            add_task_interactive.__doc__.split('\n')
        ])) or task_data,
    }

    response = None
    while response not in ('y', 'n'):
        io.task(task_data)
        response = io.prompt(
            "Import this task?",
            type=click.Choice(callbacks.keys()),
            show_choices=True,
        )

        # Execute operation
        task_data = callbacks[response]()

    if response == 'n':
        io.warn('Skipping task')
        return

    return add_task(**task_data)


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


def get_todoist_tasks(filter_task_id=None, filter_proj_id=None):
    """Return tasks from Todoist."""
    # Build filter function
    filt = {}
    if filter_task_id:
        filt['id'] = filter_task_id
    if filter_proj_id:
        filt['project_id'] = filter_proj_id
    filter_fn = make_filter_fn(filt)

    # Get all matching Todoist tasks
    tasks = todoist.items.all(filt=filter_fn)
    return tasks


def make_filter_fn(filter_dict):
    """Returns a lambda which, when given a Todoist task, will check
    whether it has the same values for keys in `filter_dict`, returning
    a bool
    """
    if not filter_dict:
        return None

    def fn(task):
        for k, v in filter_dict.items():
            if task[k] != v:
                return False
        return True

    return fn

""" Entrypoint """

if __name__ == '__main__':
    cli()
