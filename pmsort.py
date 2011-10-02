#!/usr/bin/env python

import struct
import numpy
import os
import heapq
import shutil
from itertools import islice
import multiprocessing
import tempfile
import Queue
import argparse
import sys
import atexit
import time

import logging
log = multiprocessing.log_to_stderr(level=logging.WARNING)

BUF_SIZE = 8192
SORT_MEMORY_COUNT = 1 * 1024 * 1024

class Sorter(multiprocessing.Process):
    def __init__(self, filename, queue, pill, tmpdir, sort_mem_count):
        self.filename = filename
        self.q = queue
        self.tmpdir = tmpdir
        self.sort_mem_count = sort_mem_count
        self.poison_pill = pill
        super(Sorter, self).__init__()

    def run(self):
        with open(self.filename, 'rb') as fin:
            self._do_loop(fin)

    def _do_loop(self, fin):
        try:
            i = 0
            smc = self.sort_mem_count
            while not self.poison_pill.is_set():
                try:
                    data = numpy.fromfile(file=fin, dtype=numpy.uint32, count=smc)
                except MemoryError:
                    self.poison_pill.set()
                    log.error('Cannot allocate memory for sorting')
                    return
                if not len(data):
                    break
                data.sort()
                fd, pathname = tempfile.mkstemp(dir=self.tmpdir)
                with os.fdopen(fd, 'wb') as fout:
                    data.tofile(fout)
                self.q.put(pathname)

                i += 1
                log.info('Sorted chunk {0}'.format(i))
        except Exception, ex:
            log.exception(ex)
            self.poison_pill.set()


class Merger(multiprocessing.Process):
    QUEUE_TIMEOUT = 0.5
    def __init__(self, queue, lock, pill, counter, maxcount, tmpdir, bufsize):
        self.q = queue
        self.l = lock
        self.poison_pill = pill
        self.c = counter
        self.m = maxcount
        self.tmpdir = tmpdir
        self.bs = bufsize
        super(Merger, self).__init__()
    
    def run(self):
        try:
            while not self.poison_pill.is_set() and self.c.value < self.m:
                with self.l: # Atomically take two elements from queue
                    try: a = self.q.get(timeout=self.QUEUE_TIMEOUT)
                    except Queue.Empty: continue
                    try: b = self.q.get(timeout=self.QUEUE_TIMEOUT)
                    except Queue.Empty:
                        self.q.put(a)   # Just put it back
                        continue

                new = self.merge(a, b)
                os.unlink(a)
                os.unlink(b)
                self.q.put(new)
                self.c.value += 1
                log.warn('Merged {0}/{1}'.format(self.c.value, self.m))
        except Exception, ex:
            log.exception(ex)
            self.poison_pill.set()

    def merge(self, file1, file2):
        with open(file1, 'rb') as f1, open(file2, 'rb') as f2:
            fd, pathname = tempfile.mkstemp(dir=self.tmpdir)
            with os.fdopen(fd, 'wb') as fout:
                self.write_file(fout, heapq.merge(self.read_file(f1), self.read_file(f2))) # todo write our merge
        return pathname

    def write_file(self, f, iterable):
        write_count = self.bs / 4
        for chunk in iter(lambda: ''.join(struct.pack('I', item) for item in islice(iterable, write_count)), ''):
            f.write(chunk)

    def read_file(self, f):
        read_buf = self.bs
        for chunk in iter(lambda: f.read(read_buf), ''):
            for c in xrange(len(chunk)/4):
                yield struct.unpack('I', chunk[c*4:c*4+4])[0]

class SortRunner(object):
    DEFAULT_CPUS = 4

    def __init__(self, input, output, temp=None, cpus=None, bufsize=BUF_SIZE, sort_mem_count=SORT_MEMORY_COUNT):
        self.input, self.output = input, output
        if not self.check_input():
            sys.exit('Incorrect input file')

        self.tmpdir = tempfile.mkdtemp(dir=temp)        # Temporary directory for sorted parts

        def cleanup():
            log.info('Removing dir {0}'.format(dir))
            shutil.rmtree(self.tmpdir)
        atexit.register(cleanup)                        # Do not forget to cleanup after ourselves
        
        self.queue = multiprocessing.Queue()            # Job queue
        self.lock = multiprocessing.Lock()              # Lock for queue.get synchronization
        self.counter = multiprocessing.Value('I', 0)    # Job counter
        self.chunks = self.get_chunks() - 1             # Number of jobs
        self.cpus = cpus or self._cpu_count()           # Number of merge processes
        self.bs = bufsize
        self.sort_mem_count = sort_mem_count
        self.poison_pill = multiprocessing.Event()

    def run(self):
        self.sorter = Sorter(self.input, self.queue, self.poison_pill, self.tmpdir, self.sort_mem_count)
        self.sorter.start()

        self.pool = [ Merger(self.queue, self.lock, self.poison_pill, self.counter,
                            self.chunks, self.tmpdir, self.bs) for i in range(self.cpus) ]
        map(lambda p: p.start(), self.pool)

        self.sorter.join()
        if self.sorter.exitcode < 0:
            self.poison_pill.set()
            for p in self.pool:
                p.join()
            sys.exit('Sorter finished with negative exit code {0}'.format(self.sorter.exitcode))

        while len(self.pool):
            for p in self.pool:
                if not p.is_alive():
                    if p.exitcode < 0:
                        self.poison_pill.set()
                        for p in self.pool:
                            p.join()
                        sys.exit('Merger finished with negative exit code {0}'.format(p.exitcode))
                    p.join()
                    self.pool.remove(p)
            time.sleep(1)

        if not self.poison_pill.is_set():
            self.put_result()

    def _cpu_count(self):
        """Returns number of cpus using multiprocessing with default fallback"""
        try: return multiprocessing.cpu_count()
        except NotImplementedError: return self.DEFAULT_CPUS

    def check_input(self):
        """If input file size if multiple of four"""
        return not os.path.getsize(self.input) % 4

    def get_chunks(self):
        """Number of data chunks to merge"""
        return (os.path.getsize(self.input) / 4 + SORT_MEMORY_COUNT - 1 )/ SORT_MEMORY_COUNT

    def put_result(self):
        """Move output file from tmpdir to it's location"""
        try:
            final = self.queue.get(block=False)
        except Queue.Empty:
            sys.exit('Resulting file not found.')
        else:
            shutil.move(final, self.output)

class SortValidator(object):
    def __init__(self, input, output, bufsize=BUF_SIZE):
        self.input, self.output, self.bs = input, output, bufsize
    def run(self):
        isize = os.path.getsize(self.input)
        osize = os.path.getsize(self.output)
        if isize != osize:
            sys.exit('File size differs. Found {0}, expected {1}'.format(osize, isize))
        with open(self.output, 'rb') as f:
            last = 0
            i = 0
            bs = self.bs
            for chunk in iter(lambda: f.read(bs), ''):
                for c in xrange(len(chunk)/4):
                    curr = struct.unpack_from('I', chunk[c*4:c*4+4])[0]
                    if curr < last:
                        sys.exit('Error at position {0}, values {1},{2}'.format(i, last, curr))
                    i += 1
                    last = curr

def main():
    parser = argparse.ArgumentParser(description='Sort raw binary file as an array of uint32')
    parser.add_argument('input', metavar='INPUT', help='Input file name')
    parser.add_argument('output', metavar='OUTPUT', help='Output file name')
    parser.add_argument('-bs', help='Buffer size (defaults to {0}, must be multiple of 4)'.format(BUF_SIZE), metavar='BYTES', default=BUF_SIZE)
    parser.add_argument('-j', help='Number of workers (defaults to cpu_count)', metavar='JOBS')
    parser.add_argument('-smc', help='Number of elements to sort in memory (defaults to {0})'.format(SORT_MEMORY_COUNT), default=SORT_MEMORY_COUNT)
    parser.add_argument('-t', help='Temporary directory', metavar='TMPDIR')
    parser.add_argument('-c', help='Check output file validity', action='store_true')
    args = parser.parse_args()
    
    if args.c:
        SortValidator(args.input, args.output, args.bs).run()
        print 'OK'
        return

    if args.bs % 4 or not args.smc > 0 or (args.j is not None and args.j < 0):
        sys.exit('Bad input parameter')

    try: SortRunner(args.input, args.output, args.t, args.j, args.bs, args.smc).run()
    except Exception, ex:
        log.exception(ex)
        sys.exit()

if __name__ == "__main__":
    main()
