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

Run Constructor

Runs wikipedia_revisions_constructor.py with command line arguments for input.
"""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import xml.sax
import subprocess
import argparse
from os import path
from google.cloud import bigquery as bigquery_op
import json
from .conversation_constructor import Conversation_Constructor

parser = argparse.ArgumentParser(description='Running conversation reconstruction on a list of revisions.')
parser.add_argument('--revisions',  dest='revision_ids', required = True, help='A revision list in json format for reconstruction.')
parser.add_argument('--table',  dest='table', required = True, help='BigQeury table where the revisions are stored.')
args = parser.parse_args()
UPDATE_RATE = 200

def QueryResult2json(queryresults): 
    ret = {}
    ret['sha1'] = queryresults.sha1
    ret['user_id'] = queryresults.user_id
    ret['format'] = queryresults.format
    ret['user_text'] = queryresults.user_text
    ret['timestamp'] = queryresults.timestamp
    ret['text'] = queryresults.text
    ret['page_title'] = queryresults.page_title
    ret['model'] = queryresults.model
    ret['page_namespace'] = queryresults.page_namespace
    ret['page_id'] = queryresults.page_id
    ret['rev_id'] = queryresults.rev_id
    ret['comment'] = queryresults.comment
    ret['user_ip'] = queryresults.user_ip
    ret['truncated'] = queryresults.truncated
# TODO: Part of the data doesn't have 'records_count' and record_index
#    ret['records_count'] = queryresults.records_count
#    ret['record_index'] = queryresults.record_index
    return ret

def run(revision_ids, table):
  page_history = []
  client = bigquery_op.Client(project='wikidetox-viz')
  processor = Conversation_Constructor()
  for ind, rev_id in enumerate(revision_ids):
      query = ("select * from %s where rev_id=\"%s\""%(table, rev_id))
      ret = client.query(query)
      print(ret.state)
      revision = {}
      for row in ret.result():
          revision = QueryResult2json(row)
      print(revision.keys())
      actions = processor.process(revision, DEBUGGING_MODE = False)
      for action in actions:
          print(json.dumps(action) + '\n')
  return processor.page

if __name__ == '__main__':
  run(json.loads(args.revision_ids), args.table)
