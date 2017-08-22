import json

bucket = 'gs://wikidetox-viz-dataflow'
arg_dict = {'input':'%s/ingested/enwiki-20170601-pages-meta-history*.json' % bucket, 'output':'%s/conversations/' % bucket, 'project':'wikidetox-viz', 'runner': 'DataflowRunner', 'setup_file': './setup.py', 'extra_package':'construct_utils', \
'staging_location':'%s/staging' % bucket, 'temp_location':'%s/temp' % bucket, \
'job_name':'yiqing-construction-job', 'worker_machine_type':'n1-highmem-4', 'num_workers':'6'}
with open('args_config.json', 'w') as w:
    w.write(json.dumps(arg_dict))