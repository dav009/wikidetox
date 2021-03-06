"""
Copyright 2017 Google Inc.
Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.

-------------------------------------------------------------------------------

Dataflow Main

A dataflow pipeline to ingest the Wikipedia dump from 7zipped xml files to json.

Run with:

python dataflow_main.py --setup_file ./setup.py

Args:

ingestFrom: choose from the three options : {wikipedia, local, cloud}:
  - wikipedia: performs the downloading job from Wikipedia, run with:
               [python dataflow_main.py --setup_file ./setup.py\
                   --ingestFrom=wikipedia --download --language=YourLanguage\
                   --dumpdate=YourDumpdate --cloudBucket=YourCloudBucket\
                   --project=YourGoogleCloudProject --bucket=TemporaryFileBucket]
  - local: Tests the pipeline locally, run the code with
           [python dataflow_main.py --setup_file ./setup.py\
            --ingestFrom=local --localStorage=YourLocalStorage --testmode\
            --output=YourOutputStorage --project=YourGoogleCloudProject\
            --bucket=TemporaryFileBucket]
  - cloud: Reads from downloaded bz2 files on cloud, performs the ingestion job,
    run the code with
           [python dataflow_main.py --setup_file ./setup.py\
           [--ingestFrom=cloud --output=YourOutputStorage --cloudBucket=YourCloudBucket\
           --project=YourGoogleCloudProject --bucket=TemporaryFileBucket]

output: the data storage where you want to store the ingested results
language: the language of the wikipedia data you want to extract, e.g. en, fr, zh
dumpdate: the dumpdate of the wikipedia data, e.g. latest
testmode: if turned on, the pipeline runs on DirectRunner.
localStorage: the location of the local test file.
download: if turned on, the pipeline only performs downloading job from Wikipedia.
cloudBucket: the cloud bucket where the ingestion reads from or the download stores to.
"""

from __future__ import absolute_import

import logging
import subprocess
from threading import Timer
import json
import sys
import zlib
import copy
import bz2
from os import path
from ingest_utils.wikipedia_revisions_ingester import parse_stream
import math
import os
import time
import urllib
import urllib2
import subprocess
import StringIO
import lxml

from HTMLParser import HTMLParser
import re
import argparse
import boto
import gcs_oauth2_boto_plugin
# boto needs to be configured, see here:
# https://cloud.google.com/storage/docs/boto-plugin#setup-python

import apache_beam as beam
from apache_beam.metrics.metric import Metrics
from apache_beam.options.pipeline_options import PipelineOptions
from apache_beam.options.pipeline_options import SetupOptions
from apache_beam.io import ReadFromText
from apache_beam.io import WriteToText
from apache_beam.io import filesystems
from datetime import datetime

GOOGLE_STORAGE = 'gs'
LOCAL_STORAGE = 'file'
MEMORY_THERESHOLD = 1000000


def run(known_args, pipeline_args, sections):
  """Main entry point; defines and runs the ingestion pipeline."""

  if known_args.testmode:
    # In testmode, disable cloud storage backup and run on directRunner
    pipeline_args.append('--runner=DirectRunner')
  else:
    pipeline_args.append('--runner=DataflowRunner')

  pipeline_args.extend([
      '--project={project}'.format(project=known_args.project),
    '--staging_location=gs://{bucket}/staging'.format(bucket=known_args.bucket),
    '--temp_location=gs://{bucket}/tmp'.format(bucket=known_args.bucket),
    '--job_name=ingest-latest-revisions-{lan}'.format(lan=known_args.language),
    '--num_workers=80',
  ])

  pipeline_options = PipelineOptions(pipeline_args)
  pipeline_options.view_as(SetupOptions).save_main_session = True
  with beam.Pipeline(options=pipeline_options) as p:
    pcoll = (p | "GetDataDumpList" >> beam.Create(sections))
    if known_args.download:
       pcoll = (pcoll | "DownloadDataDumps" >> beam.ParDo(DownloadDataDumps(), known_args.cloudBucket))
    else:
       pcoll = ( pcoll | "Ingestion" >> beam.ParDo(WriteDecompressedFile(), known_args.cloudBucket, known_args.ingestFrom)
            | "AddGroupByKey" >> beam.Map(lambda x: (x['year'], x))
            | "ShardByYear" >>beam.GroupByKey()
            | "WriteToStorage" >> beam.ParDo(WriteToStorage(), known_args.output, known_args.dumpdate, known_args.language))

class DownloadDataDumps(beam.DoFn):
  def process(self, element, bucket):
    """Downloads a data dump file, store in cloud storage.
       Returns the cloud storage location.
    """
    mirror, chunk_name =  element
    logging.info('USERLOG: Download data dump %s to store in cloud storage.' % chunk_name)
    # Download data dump from Wikipedia and upload to cloud storage.
    url = mirror + "/" + chunk_name
    write_path = path.join('gs://', bucket, chunk_name)
    urllib.urlretrieve(url, chunk_name)
    os.system("gsutil cp %s %s" % (chunk_name, write_path))
    os.system("rm %s" % chunk_name)
    yield chunk_name
    return

class WriteDecompressedFile(beam.DoFn):
  def __init__(self):
      self.processed_revisions = Metrics.counter(self.__class__, 'processed_revisions')
      self.large_page_revision_count = Metrics.counter(self.__class__, 'large_page_revision_cnt')

  def process(self, element, bucket, ingestFrom):
    """Ingests the xml dump into json, returns the josn records
    """
    # Decompress the data dump
    chunk_name = element
    logging.info('USERLOG: Running ingestion process on %s' % chunk_name)
    if ingestFrom == 'local':
       input_stream = chunk_name
    else:
       os.system("gsutil -m cp %s %s" % (path.join('gs://', bucket, chunk_name), chunk_name))
       input_stream = chunk_name
    # Running ingestion on the xml file
    last_revision = 'None'
    last_completed = time.time()
    cur_page_id = None
    page_size = 0
    cur_page_revision_cnt = 0
    for i, content in enumerate(parse_stream(bz2.BZ2File(chunk_name))):
      self.processed_revisions.inc()
      # Add the year field for sharding
      dt = datetime.strptime(content['timestamp'], "%Y-%m-%dT%H:%M:%SZ")
      content['year'] = dt.isocalendar()[0]
      last_revision = content['rev_id']
      yield content
      logging.info('CHUNK {chunk}: revision {revid} ingested, time elapsed: {time}.'.format(chunk=chunk_name, revid=last_revision, time=time.time() - last_completed))
      last_completed = time.time()
      if content['page_id'] == cur_page_id:
        page_size += len(json.dumps(content))
        cur_page_revision_cnt += 1
      else:
        if page_size >= MEMORY_THERESHOLD:
          self.large_page_revision_count.inc(cur_page_revision_cnt)
        cur_page_id = content['page_id']
        page_size = len(json.dumps(content))
        cur_page_revision_cnt = 1
    if page_size >= MEMORY_THERESHOLD:
      self.large_page_revision_count.inc(cur_page_revision_cnt)

    if ingestFrom != 'local': os.system("rm %s" % chunk_name)
    logging.info('USERLOG: Ingestion on file %s complete! %s lines emitted, last_revision %s' % (chunk_name, i, last_revision))

class WriteToStorage(beam.DoFn):
  def process(self, element, outputdir, dumpdate, language):
      (key, val) = element
      year = int(key)
      cnt = 0
      # Creates writing path given the year pair
      date_path = "{outputdir}/{date}-{lan}/date-{year}/".format(outputdir=outputdir, date=dumpdate, lan=language, year=year)
      file_path = 'revisions-{cnt:06d}.json'
      write_path = path.join(date_path, file_path.format(cnt=cnt))
      while filesystems.FileSystems.exists(write_path):
         cnt += 1
         write_path = path.join(date_path, file_path.format(cnt=cnt))
      # Writes to storage
      logging.info('USERLOG: Write to path %s.' % write_path)
      outputfile = filesystems.FileSystems.create(write_path)
      for output in val:
          outputfile.write(json.dumps(output) + '\n')
      outputfile.close()

class ParseDirectory(HTMLParser):
  def __init__(self):
    self.files = []
    HTMLParser.__init__(self)

  def handle_starttag(self, tag, attrs):
    self.files.extend(attr[1] for attr in attrs if attr[0] == 'href')

  def files(self):
    return self.files

def directory(mirror):
  """Download the directory of files from the webpage.
  This is likely brittle based on the format of the particular mirror site.
  """
  # Download the directory of files from the webpage for a particular language.
  parser = ParseDirectory()
  directory = urllib2.urlopen(mirror)
  parser.feed(directory.read().decode('utf-8'))
  # Extract the filenames of each XML meta history file.
  meta = re.compile('^[a-zA-Z-]+wiki-latest-pages-meta-history.*\.bz2$')
  return [(mirror, filename) for filename in parser.files if meta.match(filename)]

if __name__ == '__main__':
  logging.getLogger().setLevel(logging.INFO)
  # Define parameters
  parser = argparse.ArgumentParser()
  # Options: local, cloud
  parser.add_argument('--project',
                      dest='project',
                      help='Your google cloud project.')
  parser.add_argument('--bucket',
                      dest='bucket',
                      help='Your google cloud bucket for temporary and staging files.')
  parser.add_argument('--ingestFrom',
                      dest='ingestFrom',
                      default='none')
  parser.add_argument('--download',
                      dest='download',
                      action='store_true')
  parser.add_argument('--output',
                      dest='output',
                      help='Specify the output storage in cloud.')
  parser.add_argument('--cloudBucket',
                      dest='cloudBucket',
                      help='Specify the cloud storage location to store/stores the raw downloads.')
  parser.add_argument('--language',
                      dest='language',
                      help='Specify the language of the Wiki Talk Page you want to ingest.')
  parser.add_argument('--dumpdate',
                      dest='dumpdate',
                      help='Specify the date of the Wikipedia data dump.')
  parser.add_argument('--localStorage',
                      dest='localStorage',
                      help='If ingest from local storage, please specify the location of the input file.')
  parser.add_argument('--testmode',
                      dest='testmode',
                      action='store_true')
  known_args, pipeline_args = parser.parse_known_args()
  if known_args.download:
     # If specified downloading from Wikipedia
     dumpstatus_url = 'https://dumps.wikimedia.org/{lan}wiki/{date}/dumpstatus.json'.format(lan=known_args.language, date=known_args.dumpdate)
     try:
        response = urllib2.urlopen(dumpstatus_url)
        dumpstatus = json.loads(response.read())
        url = 'https://dumps.wikimedia.org/{lan}wiki/{date}'.format(lan=known_args.language, date=known_args.dumpdate)
        sections = [(url, filename) for filename in dumpstatus['jobs']['metahistorybz2dump']['files'].keys()]
     except:
        # In the case dumpdate is not specified or is invalid, download the
        # latest version.
        mirror = 'http://dumps.wikimedia.your.org/{lan}wiki/latest'.format(lan=known_args.language)
        sections = directory(mirror)
  if known_args.ingestFrom == "cloud":
     sections = []
     uri = boto.storage_uri(known_args.cloudBucket, GOOGLE_STORAGE)
     prefix = known_args.cloudBucket[known_args.cloudBucket.find('/')+1:]
     for obj in uri.list_bucket(prefix=prefix):
        sections.append(obj.name[obj.name.rfind('/') + 1:])
  if known_args.ingestFrom == 'local':
     sections = [known_args.localStorage]
  run(known_args, pipeline_args, sections)
