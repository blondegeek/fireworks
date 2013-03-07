#!/usr/bin/env python

"""
This module is used to submit jobs to a queue on a cluster. It can submit a single job, \
or if used in "rapid-fire" mode, can submit multiple jobs within a directory structure. \
The details of job submission and queue communication are handled using QueueParams, \
which specifies a QueueAdapter as well as desired properties of the submit script.
"""

import os
import glob
import time
from fireworks.core.fworker import FWorker
from fireworks.core.launchpad import LaunchPad
from fireworks.utilities.fw_utilities import get_fw_logger, log_exception, create_datestamp_dir
from fireworks.core.fw_constants import QUEUE_UPDATE_INTERVAL, QUEUE_RETRY_ATTEMPTS, SUBMIT_SCRIPT_NAME

__author__ = 'Anubhav Jain, Michael Kocher'
__copyright__ = 'Copyright 2012, The Materials Project'
__version__ = '0.1'
__maintainer__ = 'Anubhav Jain'
__email__ = 'ajain@lbl.gov'
__date__ = 'Dec 12, 2012'


def launch_rocket_to_queue(queue_params, launcher_dir='.', strm_lvl=None, launchpad=None, fworker=None, reserve=False):
    """
    Submit a single job to the queue.
    
    :param queue_params: A QueueParams instance
    :param launcher_dir: The directory where to submit the job
    """

    launchpad = launchpad if launchpad else LaunchPad()
    fworker = fworker if fworker else FWorker()

    # convert launch_dir to absolute path
    launcher_dir = os.path.abspath(launcher_dir)

    # initialize logger
    l_logger = get_fw_logger('queue.launcher', l_dir=queue_params.logging_dir, stream_level=strm_lvl)

    # make sure launch_dir exists:
    if not os.path.exists(launcher_dir):
        raise ValueError('Desired launch directory {} does not exist!'.format(launcher_dir))

    try:
        # get the queue adapter
        l_logger.debug('getting queue adapter')
        qa = queue_params.qa

        # move to the launch directory
        l_logger.info('moving to launch_dir {}'.format(launcher_dir))
        os.chdir(launcher_dir)

        if reserve:
            l_logger.debug('finding a FW to reserve...')
            fw, launch_id = launchpad._reserve_fw(fworker, launcher_dir)
            l_logger.debug('reserved FW with fw_id: {}'.format(fw.fw_id))
            if '_queueparams' in fw.spec:
                l_logger.debug('updating queue params using FireWork spec..')
                # TODO: make sure this does not affect future FireWorks!!
                queue_params.params.update(fw.spec['_queueparams'])
            # update the exe to include the FW_id
            queue_params.params['exe'] += ' --fw id {}'.format(fw.fw_id)

        # write and submit the queue script using the queue adapter
        l_logger.debug('writing queue script')
        with open(SUBMIT_SCRIPT_NAME, 'w') as f:
            queue_script = qa.get_script_str(queue_params, launcher_dir)
            if not queue_script:
                raise RuntimeError('queue script could not be written, check job params and queue adapter!')
            f.write(queue_script)
        l_logger.info('submitting queue script')
        # TODO: update the launch with launch_id with job id of the submitted job
        if not qa.submit_to_queue(queue_params, SUBMIT_SCRIPT_NAME):
            raise RuntimeError('queue script could not be submitted, check queue adapter and queue server status!')

    except:
        log_exception(l_logger, 'Error writing/submitting queue script!')


def rapidfire(queue_params, launch_dir='.', njobs_queue=10, njobs_block=500, strm_lvl=None, infinite=False, sleep_time=60, launchpad=None, fworker=None, reserve=False):
    """
    Submit many jobs to the queue.
    
    :param queue_params: A QueueParams instance
    :param launch_dir: directory where we want to write the blocks
    :param njobs_queue: stops submitting jobs when njobs_queue jobs are in the queue
    :param njobs_block: automatically write a new block when njobs_block jobs are in a single block
    """

    launchpad = launchpad if launchpad else LaunchPad()

    # convert launch_dir to absolute path
    launch_dir = os.path.abspath(launch_dir)

    # initialize logger
    l_logger = get_fw_logger('queue.launcher', l_dir=queue_params.logging_dir, stream_level=strm_lvl)

    # make sure launch_dir exists:
    if not os.path.exists(launch_dir):
        raise ValueError('Desired launch directory {} does not exist!'.format(launch_dir))

    try:
        l_logger.info('getting queue adapter')

        block_dir = create_datestamp_dir(launch_dir, l_logger)

        while True:
            # get number of jobs in queue
            jobs_in_queue = _get_number_of_jobs_in_queue(queue_params, njobs_queue, l_logger)
            jobs_exist = launchpad.run_exists()

            while jobs_in_queue < njobs_queue and jobs_exist:
                l_logger.info('Launching a rocket!')

                # switch to new block dir if it got too big
                if _njobs_in_dir(block_dir) >= njobs_block:
                    l_logger.info('Block got bigger than {} jobs.'.format(njobs_block))
                    block_dir = create_datestamp_dir(launch_dir, l_logger)

                # create launcher_dir
                launcher_dir = create_datestamp_dir(block_dir, l_logger, prefix='launcher_')
                # launch a single job
                launch_rocket_to_queue(queue_params, launcher_dir, strm_lvl, launchpad, fworker, reserve)
                # wait for the queue system to update
                l_logger.info('Sleeping for {} seconds...zzz...'.format(QUEUE_UPDATE_INTERVAL))
                time.sleep(QUEUE_UPDATE_INTERVAL)
                jobs_exist = not launchpad or launchpad.run_exists()
                jobs_in_queue = _get_number_of_jobs_in_queue(queue_params, njobs_queue, l_logger)

            if not infinite:
                break
            l_logger.info('Finished a round of launches, sleeping for {} secs'.format(sleep_time))
            time.sleep(sleep_time)
            l_logger.info('Checking for Rockets to run...'.format(sleep_time))

    except:
        log_exception(l_logger, 'Error with queue launcher rapid fire!')


def _njobs_in_dir(block_dir):
    """
    Internal method to count the number of jobs inside a block
    :param block_dir: the block directory we want to count the jobs in
    """
    return len(glob.glob('%s/launcher_*' % os.path.abspath(block_dir)))


def _get_number_of_jobs_in_queue(queue_params, njobs_queue, l_logger):
    """
    Internal method to get the number of jobs in the queue using the given job params. \
    In case of failure, automatically retries at certain intervals...
    
    :param queue_params: a QueueParams() instance
    :param njobs_queue: The maximum number of jobs in the queue desired
    :param l_logger: A logger to put errors/info/warnings/etc.
    """

    RETRY_INTERVAL = 30  # initial retry in 30 sec upon failure

    jobs_in_queue = queue_params.qa.get_njobs_in_queue(queue_params)
    for i in range(QUEUE_RETRY_ATTEMPTS):
        if jobs_in_queue is not None:
            l_logger.info('{} jobs in queue. Desired: {}'.format(jobs_in_queue, njobs_queue))
            return jobs_in_queue
        l_logger.warn('Could not get number of jobs in queue! Sleeping {} secs...zzz...'.format(RETRY_INTERVAL))
        time.sleep(RETRY_INTERVAL)
        RETRY_INTERVAL *= 2
        jobs_in_queue = queue_params.qa.get_njobs_in_queue(queue_params)

    raise RuntimeError('Unable to determine number of jobs in queue, check queue adapter and queue server status!')