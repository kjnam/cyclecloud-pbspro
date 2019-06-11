# Copyright (c) Microsoft Corporation. All rights reserved.
# Licensed under the MIT License.
#
import json
import logging
import os
from subprocess import check_output, check_call, CalledProcessError
import time
import unittest
import uuid

import helper
import jetpack


jetpack.util.setup_logging()

logger = logging.getLogger()

CLUSTER_USER = jetpack.config.get("cyclecloud.cluster.user.name")


def readfile_if_exist(filename):
    if not os.path.exists(filename):
        return ''
    
    with open(filename) as f:
        content = f.read()
    return content


def write_job_script():
    uid, gid, _ = helper.get_user_profile(CLUSTER_USER)
    job_script = '/shared/home/%s/run_hostname.sh' % CLUSTER_USER
    
    with open(job_script, 'w') as f:
        f.write('''
#!/bin/bash -e

cat $PBS_NODEFILE > $1
sleep 3600
'''.strip())

        os.chown(job_script, uid, gid)
        os.chmod(job_script, 0755)

    return job_script


class TestSubmit(unittest.TestCase):

    def setUp(self):
        self.userhome = "/shared/home/" + CLUSTER_USER
        self.results_dir = os.path.join(self.userhome, "test_submit")
        if not os.path.exists(self.results_dir):
            uid, gid, _ = helper.get_user_profile(CLUSTER_USER)
            os.makedirs(self.results_dir)
            os.chown(self.results_dir, uid, gid)

    def test_submissions(self):
        if jetpack.config.get("pbspro.skip_integration_tests", False):
            return
        
        job_script = write_job_script()

        def submit_job(prefix, qsub_args):
            job_uuid = prefix + str(uuid.uuid4())
            output_file_base = self.userhome + '/test_submit/%s' % job_uuid
            
            helper.sudo_check_output(['/opt/pbs/bin/qsub'] + qsub_args + ["-N", job_uuid, "--", job_script, output_file_base],
                                    CLUSTER_USER, cwd=self.userhome)
            return job_uuid

        simple = submit_job("simple-", [])
        simple_htc = submit_job("simple_htc-", [])
        multipart_select = submit_job("multipart_select-", ["-l", "select=2:ncpus=2+1:ncpus=2"])
        vscatter_excl = submit_job("vscatter_excl=", ["-l", "select=2:ncpus=1", "-l", "place=vscatter:excl"])
        smp = submit_job("smp-", ["-l", "select=2:ncpus=1", "-l", "place=pack:group=host"])
        old_nodes = submit_job("old_nodes-", ["-l", "nodes=2:ppn=2"])
        
        def _get_jobs_by_name():
            jobs = json.loads(check_output(['qstat', '-f', '-F', 'json']))
            jobs_by_name = {}
            for job_id, job in jobs.get("Jobs", {}).iteritems():
                job["Job_Id"] = job_id 
                jobs_by_name[job["Job_Name"]] = job
            return jobs_by_name
        
        def check_job(job_uuid, select, place, slot_type="execute", **resources):
            jobs_by_name = _get_jobs_by_name()
            self.assertEquals(select, jobs_by_name[job_uuid]["Resource_List"]["select"])
            self.assertEquals(place, jobs_by_name[job_uuid]["Resource_List"]["place"])
            for key in resources:
                self.assertEquals(str(resources[key]).lower(), str(jobs_by_name[job_uuid]["Resource_List"][key]).lower())
            
        check_job(simple_htc, "1:ncpus=1:ungrouped=true", "pack", ungrouped=True)
        check_job(multipart_select, "2:ncpus=2:ungrouped=false+1:ncpus=2:ungrouped=false", "group=group_id")
        check_job(vscatter_excl, "2:ncpus=1:ungrouped=false", "vscatter:excl:group=group_id")
        check_job(smp, "2:ncpus=1:ungrouped=true", "pack:group=host")
        check_job(old_nodes, "2:ncpus=2:mpiprocs=2", "scatter")
        
        hosts = {}
 
        def wait_for_results(jobs, timeout=1200):
            omega = time.time() + timeout
            at_least_one_missing = True
             
            while at_least_one_missing and time.time() < omega:
                try:
                    # continually collect host information. Some may get shutdown while we are waiting for other jobs to complete.
                    nodes = json.loads(check_output(["pbsnodes", "-a", "-F", "json"]))
                    for hostname, node in nodes["nodes"].iteritems():
                        hosts[hostname.lower()] = node
                except CalledProcessError:
                    # pbsnodes exits with 1 if there are no nodes, just ignore.
                    pass
                    
                at_least_one_missing = False
                result = {}    
                for job_id in jobs:
                    path = os.path.join(self.results_dir, job_id)
                    if not os.path.exists(path):
                        at_least_one_missing = True
                        logger.warn("%s does not exist yet." % path)
                    else:
                        with open(path) as fr:
                            result[job_id] = result.get(job_id, [])
                            for line in fr.read().splitlines():
                                hostname_short = line.strip().split(".")[0].lower()
                                if hostname_short:
                                    result[job_id].append(hostname_short)
 
                time.sleep(5)
 
            return result
 
        result = wait_for_results([simple, simple_htc, multipart_select, vscatter_excl, smp, old_nodes])
        
        def check_hosts(job_uuid, expected_procs, expected_hosts, placed):
            self.assertIn(job_uuid, result, "Job %s did not complete" % job_uuid)
            self.assertEquals(expected_procs, len(result[job_uuid]))
            
            # for some types of jobs, there is no guarantee on how many hosts they will land on.
            if expected_hosts > 0:
                self.assertEquals(expected_hosts, len(set(result[job_uuid])))
                
            for hostname in result[job_uuid]:
                hostname = hostname.lower()
                if placed:
                    self.assertIsNotNone(hosts[hostname]["resources_available"].get("group_id"))
                else:
                    self.assertIsNone(hosts[hostname]["resources_available"].get("group_id"))
                self.assertEquals(str(not placed).lower(), str(hosts[hostname]["resources_available"]["ungrouped"]).lower())
                
        check_hosts(simple, 1, 1, False)
        check_hosts(simple_htc, 1, 1, False)
        # -l place=scatter means best effort to scatter, but the jobs could land on a single machine.
        check_hosts(multipart_select, 3, -1, True)
        check_hosts(vscatter_excl, 2, 2, True)
        check_hosts(smp, 2, 1, False)
        # 4 mpi procs means 4 processes, so we will see 4 entries in the list but only 2 machines
        check_hosts(old_nodes, 4, 2, False)
        
        # now terminate the sleep jobs so that scale down can happen.
        # note, this ensures we scaled at least as many nodes as required.
        # if a node was scaled up that wasn't needed, it will fail the scale down assertions in labrat
        jobs_by_name = _get_jobs_by_name()
        for job_name in [simple, simple_htc, multipart_select, vscatter_excl, smp, old_nodes]:
            job_id = jobs_by_name[job_name]["Job_Id"]
            check_call(["qdel", job_id])
            
        # so there are 5 mpi nodes, 4 htc. This should only ever match the mpi nodes
        reuse_existing_mpi = submit_job("multipart_select-", ["-l", "select=4:ncpus=1", "-l", "place=scatter:excl"])
        
        # the nodes should already be up, so 2 mins is more than a reasonable amount of time for them to be rescheduled
        # but less than the time for a new node to be booted.
        result = wait_for_results([reuse_existing_mpi], timeout=120)
        
        check_job(reuse_existing_mpi, "4:ncpus=1:ungrouped=false", "scatter:excl:group=group_id")
        check_hosts(reuse_existing_mpi, 4, 4, True)
        
        reuse_job_id = _get_jobs_by_name()[reuse_existing_mpi]["Job_Id"]
        check_call(["qdel", reuse_job_id])


if __name__ == "__main__":
    unittest.main()
