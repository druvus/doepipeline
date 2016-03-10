"""
This module contains executors for simple pipeline execution in
a Linux-shell.
"""
from .base import BasePipelineExecutor
from .mixins import BatchExecutorMixin, ScreenExecutorMixin
import subprocess


class LocalPipelineExecutor(BatchExecutorMixin,
                            ScreenExecutorMixin,
                            BasePipelineExecutor):

    """
    Executor class running pipeline locally in a linux shell.
    """
    def __init__(self, run_in_batch=False, *args, **kwargs):
        super(LocalPipelineExecutor, self).__init__(*args, **kwargs)
        assert isinstance(run_in_batch, bool), 'run_in_batch must be boolean'

        self.run_in_batch = run_in_batch
        self.running_jobs = dict()

    def run_jobs(self, *args, **kwargs):
        if self.run_in_batch:
            self.run_batches(*args, **kwargs)
        else:
            self.run_in_screens(*args, **kwargs)

    def poll_jobs(self):
        still_running = list()
        for job_name, process in self.running_jobs.items():
            if process.poll() is None:
                still_running.append(job_name)
            else:
                if process.return_code != 0:
                    return self.JOB_FAILED, '{} has failed'.format(job_name)
                else:
                    self.running_jobs.pop(job_name)

        if still_running:
            msg = '{} still running'.format(', '.join(still_running))
            return self.JOB_RUNNING, msg
        else:
            return self.JOB_FINISHED, 'no jobs running.'

    def execute_command(self, command, watch=False, **kwargs):
        """ Execute given command by executing it in subprocess.

        Calls are made using `subprocess`-module like::

            process = subprocess.Popen(command, shell=True)

        :param str command: Command to execute.
        :param bool watch: If True, monitor process.
        :param kwargs: Keyword-arguments.
        """
        super(LocalPipelineExecutor, self).execute_command(command, watch,
                                                           **kwargs)
        process = subprocess.Popen(command, shell=True)
        if watch:
            self.running_jobs[kwargs.pop('job_name')] = process