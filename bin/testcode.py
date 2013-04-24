#!/usr/bin/env python
'''testcode2 [options] [action1 [action2...]]

testcode2 is a simple framework for comparing output from (principally numeric)
programs to previous output to reveal regression errors or miscompilation.

Run a set of actions on a set of tests.

Available actions:
  compare               compare set of test outputs from a previous testcode2
                        run against the benchmark outputs.
  compare               diff set of test outputs from a previous testcode2
                        run against the benchmark outputs.
  make-benchmarks       create a new set of benchmarks and update the userconfig
                        file with the new benchmark id.  Also runs the 'run'
                        action unless the 'compare' action is also given.
  run                   run a set of tests and compare against the benchmark
                        outputs.  Default action.
  tidy                  Remove files from previous testcode2 runs from the test
                        directories.

Requires two configuration files, jobconfig and userconfig.  See testcode2
documentation for further details.'''

# copyright: (c) 2012 James Spencer
# license: modified BSD; see LICENSE for more details

import glob
import optparse
import os
import subprocess
import sys
import threading
import time

try:
    import testcode2
except ImportError:
    # try to find testcode2 assuming it is being run directly from the source
    # layout.
    SCRIPT_DIR = os.path.abspath(os.path.dirname(sys.argv[0]))
    TESTCODE2_LIB = os.path.join(SCRIPT_DIR, '../lib/')
    sys.path.extend([TESTCODE2_LIB])
    import testcode2

import testcode2.config
import testcode2.util
import testcode2.compatibility
import testcode2.exceptions

#--- testcode initialisation ---

def init_tests(userconfig, jobconfig, test_id, reuse_id, executables=None,
        categories=None, nprocs=-1, benchmark=None, userconfig_options=None,
        jobconfig_options=None):
    '''Initialise tests from the configuration files and command-line options.

userconfig, executables, test_id and userconfig_options are passed to
testcode2.config.userconfig.

jobconfig and jobconfig_options are passed to testcode2.config.parse_jobconfig.

categories is passed to testcode2.config.select_tests.

test_id is used to set the test identifier.  If test_id is null and reused_id
is true, then the identifier is set to that of the last tests ran by testcode
otherwise a unique identifier based upon the date is used.

nprocs is the number of processors each test is run on.  If negative, the
defaults in the configuration files are used.

benchmark is the benchmark id labelling the set of benchmarks to compare the
tests too.  If None, the default in userconfig is used.

Returns:

user_options: dictionary containing user options specified in userconfig.
test_programs: dict of the test programs defined in userconfig.
tests: list of selected tests.
'''

    (user_options, test_programs) = testcode2.config.parse_userconfig(
            userconfig, executables, test_id, userconfig_options)

    # Set benchmark if required.
    if benchmark:
        for key in test_programs:
            test_programs[key].benchmark = benchmark

    (tests, test_categories) = testcode2.config.parse_jobconfig(
            jobconfig, user_options, test_programs, jobconfig_options)

    # Set number of processors...
    if nprocs >= 0:
        for test in tests:
            if not test.override_nprocs:
                test.nprocs = nprocs
            if test.nprocs < test.min_nprocs:
                test.nprocs = test.min_nprocs
            if test.nprocs > test.max_nprocs:
                test.nprocs = test.max_nprocs

    # parse selected job categories from command line
    # Remove those tests which weren't run most recently if comparing.
    if categories:
        tests = testcode2.config.select_tests(tests, test_categories,
                categories, os.path.abspath(os.path.dirname(userconfig)))

    if not test_id:
        test_id = testcode2.config.get_unique_test_id(tests, reuse_id,
                user_options['date_fmt'])
        for key in test_programs:
            test_programs[key].test_id = test_id

    return (user_options, test_programs, tests)

#--- create command line interface ---

def parse_cmdline_args(args):
    '''Parse command line arguments.

args: list of supplied arguments.

Returns:

options: object returned by optparse containing the options.
actions: list of testcode2 actions to run.
'''

    # Curse not being able to use argparse in order to support python <= 2.7!
    parser = optparse.OptionParser(usage=__doc__)

    allowed_actions = ['compare', 'run', 'diff', 'tidy', 'make-benchmarks']

    parser.add_option('-b', '--benchmark', help='Set the file ID of the '
            'benchmark files.  Default: specified in the [user] section of the '
            'userconfig file.')
    parser.add_option('-c', '--category', action='append', default=[],
            help='Select the category/group of tests.  Can be specified '
            'multiple times.  Default: use the _default_ category if run is an '
            'action unless make-benchmarks is an action.  All other cases use '
            'the _all_ category by default.  The _default_ category contains '
            'all  tests unless otherwise set in the jobconfig file.')
    parser.add_option('-e', '--executable', action='append', default=[],
            help='Set the executable(s) to be used to run the tests.  Can be '
            ' a path or name of an option in the userconfig file, in which'
            ' case all test programs are set to use that value, or in the'
            ' format program_name=value, which affects only the specified'
            ' program.')
    parser.add_option('--jobconfig', default='jobconfig', help='Set path to the'
            ' job configuration file.  Default: %default.')
    parser.add_option('--job-option', action='append', dest='job_option',
            default=[], nargs=3, help='Override/add setting to jobconfig.  '
            'Takes three arguments.  Format: section_name option_name value.  '
            'Default: none.')
    parser.add_option('--older-than', type='int', dest='older_than', default=14,
            help='Set the age (in days) of files to remove.  Only relevant to '
            'the tidy action.  Default: %default days.')
    parser.add_option('-p', '--processors', type='int', default=-1,
            dest='nprocs', help='Set the number of processors to run each test '
            'on.  Default: use settings in configuration files.')
    parser.add_option('-q', '--quiet', action='store_false', dest='verbose',
            default=True, help='Print only minimal output.  Default: False.')
    parser.add_option('-s', '--submit', dest='queue_system', default=None,
            help='Submit tests to a queueing system of the specified type.  '
            'Only PBS system is currently implemented.  Default: %default.')
    parser.add_option('-t', '--test-id', dest='test_id', help='Set the file ID '
            'of the test outputs.  Default: unique filename based upon date '
            'if running tests and most recent test_id if comparing tests.')
    parser.add_option('--total-processors', type='int', default=-1,
            dest='tot_nprocs', help='Set the total number of processors to use '
            'to run tests concurrently.  Relevant only to the run option.  '
            'Default: run all tests concurrently run if --submit is used; run '
            'tests sequentially otherwise.')
    parser.add_option('--userconfig', default='userconfig', help='Set path to '
            'the user configuration file.  Default: %default.')
    parser.add_option('--user-option', action='append', dest='user_option',
            default=[], nargs=3, help='Override/add setting to userconfig.  '
            'Takes three arguments.  Format: section_name option_name value.  '
            'Default: none.')

    (options, args) = parser.parse_args(args)

    # Default action.
    if not args or ('make-benchmarks' in args and 'compare' not in args
            and 'run' not in args):
        # Run tests by default if no action provided.
        # Run tests before creating benchmark by default.
        args.append('run')

    # Default category.
    if not options.category:
        # We quietly filter out tests which weren't run last when diffing
        # or comparing.
        options.category = ['_all_']
        if 'run' in args and 'make-benchmarks' not in args:
            options.category = ['_default_']

    test_args = (arg not in allowed_actions for arg in args)
    if testcode2.compatibility.compat_any(test_args):
        print('At least one action is not understood: %s.' % (' '.join(args)))
        parser.print_usage()
        sys.exit(1)

    # Parse executable option to form dictionary in format expected by
    # parse_userconfig.
    exe = {}
    for item in options.executable:
        words = item.split('=')
        if len(words) == 1:
            # setting executable for all programs (unless otherwise specified)
            exe['_tc_all'] = words[0]
        else:
            # format: program_name=executable
            exe[words[0]] = words[1]
    options.executable = exe

    # Set FILESTEM if test_id refers to a benchmark file or the benchmark
    # refers to a test_id.
    filestem = testcode2.FILESTEM.copy()
    if options.benchmark and options.benchmark[:2] == 't:':
        filestem['benchmark'] = testcode2.FILESTEM['test']
        options.benchmark = options.benchmark[2:]
    if options.test_id and options.test_id[:2] == 'b:':
        filestem['test'] = testcode2.FILESTEM['benchmark']
        options.test_id = options.test_id[2:]
    if filestem['test'] != testcode2.FILESTEM['test'] and 'run' in args:
        print('Not allowed to set test filename to be a benchmark filename '
                'when running calculations.')
        sys.exit(1)
    testcode2.FILESTEM = filestem.copy()

    # Convert job-options and user-options to dict of dicsts format.
    for item in ['user_option', 'job_option']:
        uj_opt = getattr(options, item)
        opt = dict( (section, {}) for section in
                testcode2.compatibility.compat_set(opt[0] for opt in uj_opt) )
        for (section, option, value) in uj_opt:
            opt[section][option] = value
        setattr(options, item, opt)

    return (options, args)

#--- actions ---

def run_tests(tests, verbose, cluster_queue=None, tot_nprocs=0):
    '''Run tests.

tests: list of tests.
verbose: print verbose output if true.
cluster_queue: name of cluster system to use.  If None, tests are run locally.
    Currently only PBS is implemented.
tot_nprocs: total number of processors available to run tests on.  As many
    tests (in a LIFO fashion from the tests list) are run at the same time as
    possible without using more processors than this value.  If less than 1 and
    cluster_queue is specified, then all tests are submitted to the cluster at
    the same time.  If less than one and cluster_queue is not set, then
    tot_nprocs is ignored and the tests are run sequentially (default).
'''
    def run_test_worker(semaphore, semaphore_lock, test, *run_test_args):
        '''Launch a test after waiting until resources are available to run it.

semaphore: threading.Semaphore object containing the number of cores/processors
    which can be used concurrently to run tests.
semaphore.lock: threading.Lock object used to restrict acquiring the semaphore
    to one thread at a time.
test: test to run.
run_test_args: arguments to pass to test.run_test method.
'''

        # Ensure that only one test attempts to register resources with the
        # semaphore at a time.  This restricts running the tests to a LIFO
        # fashion which is not perfect (we don't attempt to backfill with
        # smaller tests, for example) but is a reasonable and (most
        # importantly) simple first-order approach.
        semaphore_lock.acquire()
        # test.nprocs is <1 when program is run in serial.
        nprocs_used = max(1, test.nprocs)
        for i in range(nprocs_used):
            semaphore.acquire()
        semaphore_lock.release()

        test.run_test(*run_test_args)

        for i in range(nprocs_used):
            semaphore.release()

    if tot_nprocs <= 0 and cluster_queue:
        # Running on cluster.  Default to submitting all tests at once.
        tot_nprocs = sum(test.nprocs for test in tests)

    if tot_nprocs > 0:
        # Allow at most tot_nprocs cores to be used at once by tests.
        max_test_nprocs = max(test.nprocs for test in tests)
        if max_test_nprocs > tot_nprocs:
            err = ('Number of available cores less than the number required by '
                   'the largest test: at least %d needed, %d available.'
                   % (max_test_nprocs, tot_nprocs))
            raise testcode2.exceptions.TestCodeError(err)
        semaphore = threading.BoundedSemaphore(tot_nprocs)
        slock = threading.Lock()
        jobs = [threading.Thread(
                    target=run_test_worker,
                    args=(semaphore, slock, test, verbose, cluster_queue)
                                )
                    for test in tests]
        for job in jobs:
            job.start()
        for job in jobs:
            job.join()
    else:
        # run straight through, one at a time
        for test in tests:
            test.run_test(verbose, cluster_queue)


def compare_tests(tests, verbose):
    '''Compare tests.

tests: list of tests.
verbose: print verbose output if true.
'''

    nskipped = 0

    for test in tests:
        for (inp, args) in test.inputs_args:
            test_file = testcode2.util.testcode_filename(
                    testcode2.FILESTEM['test'],
                    test.test_program.test_id, inp, args
                    )
            test_file = os.path.join(test.path, test_file)
            if os.path.exists(test_file):
                test.verify_job(inp, args, verbose)
            else:
                if verbose:
                    print('Skipping comparison.  '
                          'Test file does not exist: %s.\n' % test_file)
                nskipped += 1

    return nskipped

def diff_tests(tests, diff_program, verbose):
    '''Diff tests.

tests: list of tests.
diff_program: diff program to use.
verbose: print verbose output if true.
'''

    for test in tests:
        cwd = os.getcwd()
        os.chdir(test.path)
        for (inp, args) in test.inputs_args:
            benchmark = testcode2.util.testcode_filename(
                    testcode2.FILESTEM['benchmark'],
                    test.test_program.benchmark, inp, args
                    )
            test_file = testcode2.util.testcode_filename(
                    testcode2.FILESTEM['test'],
                    test.test_program.test_id, inp, args
                    )
            if verbose:
                print('Diffing %s and %s in %s.' % (benchmark, test_file,
                    test.path))
            if not os.path.exists(test_file) or not os.path.exists(benchmark):
                if verbose:
                    print('Skipping diff: %s does not exist.' % test_file)
            else:
                diff_cmd = '%s %s %s' % (diff_program, benchmark, test_file)
                diff_popen = subprocess.Popen(diff_cmd, shell=True)
                diff_popen.wait()
        os.chdir(cwd)

def tidy_tests(tests, ndays, submit_templates=None):
    '''Tidy up test directories.

tests: list of tests.
ndays: test files older than ndays are deleted.
submit_templates: list of submit templates used in submitting tests to
    a cluster.  The submit files created from the templates are also deleted.
'''

    epoch_time = time.time() - 86400*ndays

    test_globs = ['test.out*','test.err*']
    if submit_templates:
        test_globs.extend(['%s*' % tmpl for tmpl in submit_templates])

    print(
            'Delete all %s files older than %s days from each job directory?'
                % (' '.join(test_globs), ndays)
         )
    ans = ''
    while ans != 'y' and ans != 'n':
        ans = testcode2.compatibility.compat_input('Confirm [y/n]: ')

    if ans == 'n':
        print('No files deleted.')
    else:
        for test in tests:
            cwd = os.getcwd()
            os.chdir(test.path)
            for test_glob in test_globs:
                for test_file in glob.glob(test_glob):
                    if os.stat(test_file)[-2] < epoch_time:
                        os.remove(test_file)
            os.chdir(cwd)

def make_benchmarks(test_programs, tests, userconfig, copy_files_since):
    '''Make a new set of benchmarks.

test_programs: dictionary of test programs.
tests: list of tests.
userconfig: path to the userconfig file.  This is updated with the new benchmark id.
copy_files_since: files produced since the timestamp (in seconds since the
    epoch) are copied to the testcode_data subdirectory in each test.
'''

    # All tests passed?
    statuses = [test.get_status() for test in tests]
    npassed = sum(status[0] for status in statuses)
    nwarning = sum(status[1] for status in statuses)
    nran = sum(status[2] for status in statuses)
    if npassed != nran:
        ans = ''
        print('Not all tests passed.')
        while ans != 'y' and ans != 'n':
            ans = testcode2.compatibility.compat_input(
                                                'Create new benchmarks? [y/n] ')
        if ans != 'y':
            return None

    # Get vcs info.
    vcs = {}
    for (key, program) in test_programs.items():
        if program.vcs and program.vcs.vcs:
            vcs[key] = program.vcs.get_code_id()
        else:
            print('Program not under (known) version control system')
            vcs[key] = testcode2.compatibility.compat_input(
                    'Enter revision id for %s: ' % (key))

    # Benchmark label from vcs info.
    if len(vcs) == 1:
        benchmark = vcs.popitem()[1]
    else:
        benchmark = []
        for (key, code_id) in vcs.items():
            benchmark.append('%s-%s' % (key, code_id))
        benchmark = '.'.join(benchmark)

    # Create benchmarks.
    for test in tests:
        test.create_new_benchmarks(benchmark, copy_files_since)

    # update userconfig file.
    if userconfig:
        print('Setting new benchmark in userconfig to be %s.' % (benchmark))
        config = testcode2.compatibility.configparser.RawConfigParser()
        config.optionxform = str # Case sensitive file.
        config.read(userconfig)
        config.set('user', 'benchmark', benchmark)
        userconfig = open(userconfig, 'w')
        config.write(userconfig)
        userconfig.close()

#--- info output ---

def start_status(tests, running, verbose):
    '''Print a header containing useful information.

tests: list of tests.
running: true if tests are to be run.
verbose: true if output is required; if false no output is produced.
'''

    if verbose:
        exes = [test.test_program.exe for test in tests]
        exes = testcode2.compatibility.compat_set(exes)
        if running:
            for exe in exes:
                print('Using executable: %s.' % (exe))
        # All tests use the same test_id and benchmark.
        print('Test id: %s.' % (tests[0].test_program.test_id))
        print('Benchmark: %s.' % (tests[0].test_program.benchmark))
        print('')

def end_status(tests, skipped=0, verbose=True):
    '''Print a footer containing useful information.

tests: list of tests.
skipped: number of tests skipped (ie not run or compared).
verbose: if true additional output is produced; if false a minimal status is
    produced containing the same amount of output.
'''

    statuses = [test.get_status() for test in tests]
    npassed = sum(status[0] for status in statuses)
    nwarning = sum(status[1] for status in statuses)
    nran = sum(status[2] for status in statuses)
    # Treat warnings as passes but add a note about how many warnings.
    npassed += nwarning

    # Pedantic.
    if nwarning == 1:
        warning = 'warning'
    else:
        warning = 'warnings'
    if nran == 1:
        test = 'test'
    else:
        test = 'tests'

    if skipped != 0 and nwarning != 0:
        add_info_msg = ' (%s %s, %s skipped)' % (nwarning, warning, skipped)
    elif skipped != 0:
        add_info_msg = ' (%s skipped)' % (skipped,)
    elif nwarning != 0:
        add_info_msg = ' (%s %s)' % (nwarning, warning)
    else:
        add_info_msg = ''

    if verbose:
        msg = 'All done.  %s%s out of %s %s passed%s.'
        if npassed == nran:
            print(msg % ('', npassed, nran, test, add_info_msg))
        else:
            print(msg % ('ERROR: only ', npassed, nran, test, add_info_msg))
    else:
        print(' [%s/%s%s]'% (npassed, nran, add_info_msg))

    # ternary operator not in python 2.4. :-(
    ret_val = 0
    if nran != npassed:
        ret_val = 1

    return ret_val

#--- main runner ---

def main(args):
    '''main controller procedure.

args: command-line arguments passed to testcode2.
'''

    start_time = time.time()

    (options, actions) = parse_cmdline_args(args)

    # Shortcut names to options used multiple times.
    verbose = options.verbose
    userconfig = options.userconfig
    reuse_id = ( ('compare' in actions or 'diff' in actions)
                 and not 'run' in actions )

    (user_options, test_programs, tests) = init_tests(userconfig,
            options.jobconfig, options.test_id, reuse_id,
            options.executable, options.category, options.nprocs,
            options.benchmark, options.user_option,
            options.job_option)

    ret_val = 0
    if not (len(actions) == 1 and 'tidy' in actions):
        start_status(tests, 'run' in actions, verbose)
    if 'run' in actions:
        run_tests(tests, verbose, options.queue_system, options.tot_nprocs)
        ret_val = end_status(tests, 0, verbose)
    if 'compare' in actions:
        nskipped = compare_tests(tests, verbose)
        ret_val = end_status(tests, nskipped, verbose)
    if 'diff' in actions:
        diff_tests(tests, user_options['diff'], verbose)
    if 'tidy' in actions:
        submit_templates = []
        for test_program in test_programs.values():
            if test_program.submit_template:
                submit_templates.append(test_program.submit_template)
        tidy_tests(tests, options.older_than, submit_templates)
    if 'make-benchmarks' in actions:
        make_benchmarks(test_programs, tests, userconfig, start_time)

    return ret_val

if __name__ == '__main__':

    sys.exit(main(sys.argv[1:]))
