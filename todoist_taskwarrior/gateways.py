import logging

from todoist.api import TodoistAPI
from taskw import TaskWarrior as TW
from . import utils, io

TODOIST_CACHE = '~/.todoist-sync/'


class Todoist:

    def __init__(self, api_key):
        self.todoist = TodoistAPI(api_key, cache=TODOIST_CACHE)

    def get_tasks(self, filter_task_id=None, filter_proj_id=None):
        """Return tasks from Todoist."""
        # Build filter function
        filt = {}
        if filter_task_id:
            filt['id'] = filter_task_id
        if filter_proj_id:
            filt['project_id'] = filter_proj_id
        filter_fn = make_filter_fn(filt)

        # Get all matching Todoist tasks
        tasks = self.todoist.items.all(filt=filter_fn)
        return tasks

    def sync(self):
        """TODO: Should not be exposed to external API."""
        self.todoist.sync()

    def project_name_from_todoist(self, project_id, map_project):
        # Project
        p = self.todoist.projects.get_by_id(project_id)
        logging.debug(f"GET_PROJECT_BY_ID project_id={project_id} project={p}")
        project_name = ''
        if p:
            project_hierarchy = [p]
            while p['parent_id']:
                p = self.todoist.projects.get_by_id(p['parent_id'])
                project_hierarchy.insert(0, p)
                logging.debug(f"PROJECT_HIERARCHY parent_id={p['parent_id']} hierarchy={project_hierarchy}")
            project_name = '.'.join(p['name'] for p in project_hierarchy)
            logging.debug(f'PROJECT_HIERARCHY project_name={project_name}')

            project_name = utils.try_map(
                map_project,
                project_name
            )
        return project_name


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


TW_STATUS_PENDING = "pending"
TW_STATUS_COMPLETED = "completed"


class TaskWarrior:
    def __init__(self, config_file):
        self.client = TW(
            config_filename=config_file,
            config_overrides={'uda.todoist_id.type': 'string'},
        )

    def update(self, task, data):
        """Update given task with data."""
        keys = "description due project".split()
        for key in keys:
            task[key] = data[key]
        self.client.task_update(task)

    def get_pending_tasks(self):
        """Return pending TaskWarrior tasks.

        This does not include tasks which are waiting to be displayed (pending
        but not displayed in TaskWarrior because current date hasn't reached
        the "Waiting" date yet).
        """
        return self.client.filter_tasks({"status": TW_STATUS_PENDING})

    def get_task(self, tid):
        """ Given a Todoist ID, check if the task exists """
        _, task = self.client.get_task(todoist_id=tid)
        return task

    def add_task(self,
                 tid, description, project, tags, priority, entry, due, recur):
        """Add a taskwarrior task from todoist task

        Returns the taskwarrior task.
        """
        with io.with_feedback(f"Importing '{description}' ({project})"):
            return self.client.task_add(
                description,
                project=project,
                tags=tags,
                priority=priority,
                entry=entry,
                due=due,
                recur=recur,
                todoist_id=tid,
            )
