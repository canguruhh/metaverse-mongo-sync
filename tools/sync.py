#!/bin/env python
#coding=utf-8
import pymongo
from pymongo.errors import ConnectionFailure
import requests
import logging
import signal
import json
import collections
import time
import sys
import httplib
import urllib

import os

time.sleep( 10 )

mongodb_host = os.environ['MONGO_HOST']
mongodb_port = int(os.environ['MONGO_PORT'])
db_name = os.environ['MONGO_DB']
rpc_uri = 'http://%s:%s/rpc' % (os.environ['MVSD_HOST'], os.environ['MVSD_PORT'])
######global asset######
global_asset = {}

max_try = 10

null_hash = '0' * 64


def set_up_logging():
    logging.getLogger("requests").setLevel(logging.WARNING)
    logging.basicConfig(level=logging.DEBUG,
                format='%(asctime)s %(filename)s[line:%(lineno)d] %(levelname)s %(message)s',
                datefmt='%a, %d %b %Y %H:%M:%S',
                filename='block_sync.log',
                filemode='a+')
    console = logging.StreamHandler()
    console.setLevel(logging.INFO)
    formatter = logging.Formatter('%(asctime)s - %(name)s - %(message)s')
    console.setFormatter(formatter)
    logging.getLogger('').addHandler(console)

set_up_logging()


class Boolean:
    def __init__(self):
        self.__value = False

    def __call__(self, *args, **kwargs):
        if len(args) > 1:
            raise RuntimeError('undefined action')

        if not args:
            return self.__value

        self.__value = bool(args[0])


class HeaderNotFound(Exception):
    def __init__(self, *args, **kwargs):
        Exception.__init__(self, *args, **kwargs)


class TryException(Exception):
    def __init__(self, *args, **kwargs):
        Exception.__init__(self, *args, **kwargs)


class RpcConnectionException(Exception):
    def __init__(self, *args, **kwargs):
        Exception.__init__(self, *args, **kwargs)


class MongodbConnectionException(Exception):
    def __init__(self, *args, **kwargs):
        Exception.__init__(self, *args, **kwargs)


class CriticalException(Exception):
    def __init__(self, *args, **kwargs):
        Exception.__init__(self, *args, **kwargs)



def new_mongo(host, port, db_name_):
    conn = pymongo.MongoClient(host, port)
    try:
        conn.admin.command('ismaster')
    except ConnectionFailure as e:
        logging.error('mongodb connection error,%s' % e)
        raise e

    return getattr(conn, db_name_)


class RpcClient:

    def __init__(self, uri):
        self.__uri = uri

    def __request(self, cmd_, params):
        if not isinstance(params, list):
            raise RuntimeError('bad params type')

        cmd_ = cmd_.replace('_', '-')
        content = {'method':cmd_, 'params':params}
        resp = requests.post(self.__uri, data=json.dumps(content))
        if resp.status_code != 200:
            raise RuntimeError('bad request,%s' % resp.status_code)

        return resp.text

    def __getattr__(self, item):
        return lambda params:self.__request(item, params)


def new_rpcclient(uri):
    return RpcClient(uri)


def set_up_signal(code):
    def handle_sig(s, f):
        logging.info('receive signal %s, begin to exit...' % s)
        code(True)

    signal.signal(signal.SIGTERM, handle_sig)
    signal.signal(signal.SIGINT, handle_sig)


def dirty_worker(db_chain, block, transactions, addresses, outs, asset_outs, inputs, new_txs):
    if not transactions or not outs or not inputs:
        raise RuntimeError('transaction,outputs, ')

    # try:
    tb_block = db_chain.block
    tb_block.insert_one(block)

    tb_tx = db_chain.transaction
    tb_tx.insert_many(transactions)

    addrs = [{'address':a, 'id':a_id} for a, a_id in addresses.items()]

    if addrs:
        tb_addr = db_chain.address
        tb_addr.insert_many(addrs)

    tb_output = db_chain.output
    tb_output.insert_many(outs)

    tb_input = db_chain.input
    tb_input.insert_many(inputs)

    tb_asset_output = db_chain.asset_output
    if asset_outs:
        tb_asset_output.insert_many(asset_outs)

    tb_new_tx = db_chain.tx
    tb_new_tx.insert_many(new_txs)

    # except Exception as e:
    #     logging.error('mongodb exception,%s' % e)


def parse_lock_height(script):
    import re
    lock_height = 0
    p = '\[([\\w| ]+)\] numequalverify dup hash160 \[[\\w| ]+\] equalverify checksig'
    m = re.match(p, script)
    if m and m.groups():
        def set_vch(h):
            import binascii
            data_chunk = binascii.unhexlify(h.encode('utf8'))
            res = 0
            if not data_chunk:
                return res
            for i, d in enumerate(data_chunk):
                res |= (ord(d) << (8*i) )
            if ord(d[-1]) & 0x80:
                return -(res & ~(0x80 << (8*(len(data_chunk)-1))) )
            return res
        s_lock_height = m.groups()[0].strip()
        lock_height = set_vch(s_lock_height)
    return lock_height


def input_hash_to_output_id(db_chain, hashes):
    hashes = filter(lambda x: False if x[0] == null_hash else True, hashes)
    if not hashes:
        return {null_hash: [-1, 4294967295]}
    outputs = []
    for h in hashes:
        cur = db_chain.output.find({'hash': h[0], 'index':h[1], 'asset':h[2]})
        outputs.extend([c for c in cur])
    res = {'%s_%s_%s' % (output['hash'], output['index'], output['asset']):[output['tx_id'], output['address_id'], output['value'], output['index'], output['asset'], output['decimal_number'], parse_lock_height(output['script']) ] for output in outputs}
    return res


def rpc_network_check(func):
    def wrapper(*args, **kwargs):
        try:
            res = func(*args, **kwargs)
        except Exception as e:
            global max_try
            logging.info('try %s' % max_try)
            max_try -= 1
            if max_try <= 0:
                raise RpcConnectionException('after 10 tries')
            raise TryException('try %s' % max_try)
        if res.find('timed out') > 0:
            raise RpcConnectionException('%s request failed,block height %s' % (func.__name__, 0))
        return res
    return wrapper


@rpc_network_check
def get_header(rpc, height):
    return rpc.fetch_header(['-t', str(height)])


@rpc_network_check
def get_block(rpc, hash_, json_or_not = False):
    return rpc.getblock([hash_, '--json=%s' % json_or_not])


def sync_block(db_chain, block_height, tx_id, o_id, i_id, addresses, rpc):
    new_txs = []
    addresses_ = {}
    #for key in global_asset:
    #   print(key, global_asset[key])
    res = get_header(rpc, block_height)
    try:
        res = json.loads(res)
        if res.get('error') is not None or res.get('result') is None:
            raise HeaderNotFound('%s' % res)
    except Exception as e:
        logging.info('getheader json loads exception,%s,%s, %s' % (block_height, e, res))
        raise HeaderNotFound('header %s not found' % block_height)
    hash_ = res['result']['hash']

    block_ = get_block(rpc, hash_, 'true')
    block_ = json.loads(block_)

    header = block_['header']['result']
    header['number'] = int(header['number'])
    txs_ = block_['txs']['transactions']
    txs = [tx['hash'] for tx in txs_]

    header['txs'] = txs
    block = header

    transactions = []
    outs = []
    asset_outs = []
    inputs = []
    asset_pre = ['ETP']
    hashes = []
    block_tx_hash = {}

    block_tx_output_value = {}
    for tx in txs_:
        new_tx = {}
        transactions_ = {}
        tx_hash = tx['hash']
        transactions_['hash'] = tx_hash

        block_tx_hash[tx_hash] = tx_id

        transactions_['tx_id'] = tx_id
        new_tx['hash'] = tx_hash
        new_tx['height'] = block_height
        new_tx['timestamp'] = header['time_stamp']

        outputs = tx['outputs']
        inputs_ = tx['inputs']

        for o in outputs:
            if 'asset-transfer' == o['attachment']['type']:
                asset_name = o['attachment']['symbol']
                asset_pre.append(asset_name)

        new_asset_pre = list(set(asset_pre))
        for i in inputs_:
            for a in new_asset_pre:
                hashes.append((i['previous_output']['hash'], int(i['previous_output']['index']), a))
        hash_ids = input_hash_to_output_id(db_chain, hashes)
        new_inputs = []
        new_outputs = []
        for i in inputs_:
            for a in new_asset_pre:
                p = i['previous_output']
                pre_hash = p['hash']
                pre_index = int(p['index'])
                hash_index = '%s_%s_%s' % (pre_hash, pre_index, a)
                if pre_hash != null_hash and hash_index not in hash_ids and hash_index not in block_tx_output_value:
                    #raise CriticalException('previous output hash not found,%s', p)
                    continue

                if pre_hash == null_hash:
                    pre_ids = [-1, -1, 0, -1, 'ETP', '8', 0]
                else:
                    if hash_index in hash_ids:
                        pre_ids = hash_ids[hash_index]
                    else:
                        pre_ids = block_tx_output_value[hash_index]
                    # pre_ids = [tx_id_value[0], tx_id_value[1], pre_hash, tx_id_value[2]]

            # pre_ids = hash_ids[pre_hash] if pre_hash in hash_ids else [-1, -1, 0]
            # pre_tx_id = block_tx_hash[pre_hash] if pre_hash in block_tx_hash else pre_ids[0]
                pre_tx_id = pre_ids[0]
                address_id = pre_ids[1]
                tx_value = pre_ids[2]
                asset = pre_ids[4]
                decimal_number = pre_ids[5]
                lock_height_ = pre_ids[6]
                inputs.append({'input_id': i_id, 'script': i['script'], 'belong_tx_id': pre_tx_id, 'tx_value': tx_value, 'address_id':address_id, 'output_index': pre_index, 'tx_id':tx_id, 'asset':asset, 'decimal_number':decimal_number})
                i_id += 1
                pre_address = i['address'] if i.get('address') is not None else ''
                new_inputs.append({'address':pre_address, 'quantity':tx_value, 'type':'transfer', 'index':pre_ids[3], 'asset':{'symbol':asset, 'decimals':int(decimal_number)}, 'lock_height':lock_height_})

        for o in outputs:
            if o.get('address') is None:
                addr = ''
            else:
                addr = o['address']
            if addr not in addresses:
                a_id = len(addresses) + 1
                addresses[addr] = a_id
                addresses_[addr] = a_id
            else:
                a_id = addresses[addr]
            #block_tx_output_value['%s_%s' % (tx_hash, o['index'])] = (tx_id, a_id, int(o['value']), int(o['index']))

            if o['attachment']['type'] == 'asset-issue':
                asset_name = o['attachment']['symbol']
                asset_amount = int(o['attachment']['quantity'])
                asset_type = o['attachment'].get('decimal_number') if o['attachment'].get('decimal_number') is not None else o['attachment']['asset_type']
                global_asset[asset_name] = asset_type
                asset_outs.append({'hash': tx_hash, 'address_id': a_id
                                      , 'output_id': o_id, 'value': asset_amount
                                      , 'asset': asset_name, 'index': int(o['index']), 'tx_id': tx_id
                                      , 'description': o['attachment']['description']
                                      , 'asset_type': asset_type,
                                   'issuer': o['attachment']['issuer']})
                outs.append({'hash': tx_hash, 'address_id': a_id, 'decimal_number': int(asset_type),
                             'script': o['script'], 'output_id': o_id, 'value': asset_amount, 'asset': asset_name,
                             'index': int(o['index']), 'tx_id': tx_id})
                block_tx_output_value['%s_%s_%s' % (tx_hash, o['index'], asset_name)] = (
                tx_id, a_id, int(o['attachment']['quantity']), int(o['index']), asset_name,
                asset_type, 0)
                new_outputs.append({'address':o['address'], 'quantity':asset_amount, 'type':'issue', 'index':o['index'], 'asset':{'symbol':asset_name, 'decimals':int(asset_type)}, 'lock_height': 0})
                if o['value'] == '0':
                    o_id += 1
                    continue
                o_id += 1
            elif 'asset-transfer' == o['attachment']['type']:
                asset_name = o['attachment']['symbol']
                asset_amount = int(o['attachment']['quantity'])
                asset_type = global_asset[asset_name]
                asset_pre.append(asset_name)
                outs.append({'hash': tx_hash, 'address_id': a_id, 'script': o['script'], 'output_id': o_id,
                             'value': asset_amount
                                , 'asset': asset_name, 'index': int(o['index']), 'tx_id': tx_id,
                             'decimal_number': global_asset[asset_name]})
                block_tx_output_value['%s_%s_%s' % (tx_hash, o['index'], asset_name)] = (
                    tx_id, a_id, int(asset_amount), int(o['index']), asset_name, global_asset[asset_name], 0)
                o_id += 1
                new_outputs.append({'address':o['address'], 'quantity':asset_amount, 'type':'transfer', 'index':o['index'], 'asset':{'symbol':asset_name, 'decimals':int(asset_type)}, 'lock_height': 0})
                if o['value'] == '0':
                    continue
            asset_name = 'ETP'
            outs.append(
                {'hash': tx_hash, 'address_id': a_id, 'script': o['script'], 'output_id': o_id, 'value': int(o['value']),
                 'asset': asset_name, 'index': int(o['index']), 'tx_id': tx_id, 'decimal_number': '8'})
            lock_height = parse_lock_height(o['script'])
            block_tx_output_value['%s_%s_%s' % (tx_hash, o['index'], asset_name)] = (
                tx_id, a_id, int(o['value']), int(o['index']), asset_name, '8', lock_height)

            new_outputs.append({'address': '' if o.get('address') is None else o['address'], 'quantity':o['value'], 'type':'transfer', 'index':o['index'], 'asset':{'symbol':'ETP', 'decimals':8}, 'lock_height': lock_height})
            o_id += 1

        new_tx['id'] = tx_id
        new_tx['inputs'] = new_inputs
        new_tx['outputs'] = new_outputs
        new_txs.append(new_tx)

        transactions_['block_height'] = block_height
        transactions.append(transactions_)
        tx_id += 1

    return block, transactions, addresses_, outs, asset_outs, inputs, new_txs


def process_batch(db_chain, batch_info):
    block_height = batch_info['block_height']
    tx_id = batch_info['tx_id']
    input_id = batch_info['input_id']
    output_id = batch_info['output_id']
    try:
        db_chain.block.remove({'number': {'$gte': block_height}})
        db_chain.transaction.remove({'tx_id': {'$gte': tx_id}})
        db_chain.output.remove({'output_id': {'$gte': output_id}})
        db_chain.input.remove({'input_id': {'$gte': input_id}})
    except Exception as e:
        logging.info('process batch exception,%s' % e)
        raise e


def get_last_height(db_chain):
    batches = db_chain.batch.find()
    map(lambda x: process_batch(db_chain, x), batches)

    db_chain.batch.remove()

    block_height = db_chain.block.find({}).sort('number', -1).limit(1)
    max_tx_id = db_chain.transaction.find().sort('tx_id', -1).limit(1)
    max_output_id = db_chain.output.find({}).sort('output_id', -1).limit(1)
    max_input_id = db_chain.input.find({}).sort('input_id', -1).limit(1)

    block_height_count = block_height.count()

    return block_height.next()['number'] + 1 if block_height_count > 0  else 0\
        , max_tx_id.next()['tx_id'] + 1 if max_tx_id.count() > 0 else 0\
        , max_output_id.next()['output_id'] + 1 if max_output_id.count() > 0 else 0\
        , max_input_id.next()['input_id'] + 1if max_input_id.count() > 0 else 0


def clear_fork(db_chain, fork_height):
    blocks = db_chain.block.find({'number':{'$gte':fork_height}})
    block_heights = [b['number'] for b in blocks]
    txs = db_chain.transaction.find({'block_height':{'$in':block_heights}})
    new_txs = db_chain.tx.find({'block_height':{'$in':block_heights}})
    new_tx_ids = [t['id'] for t in new_txs]
    tx_ids = [t['tx_id'] for t in txs]
    tx_inputs = db_chain.input.find({'tx_id':{'$in':tx_ids}})
    tx_outputs = db_chain.output.find({'tx_id':{'$in':tx_ids}})

    tx_input_ids = [t['input_id'] for t in tx_inputs]
    tx_output_ids = [t['output_id'] for t in tx_outputs]

    db_chain.block.remove({'number':{'$in':block_heights}})
    db_chain.transaction.remove({'tx_id':{'$in':tx_ids}})
    db_chain.tx.remove({'id':{'$in':new_tx_ids}})
    db_chain.input.remove({'input_id':{'$in':tx_input_ids}})
    db_chain.output.remove({'output_id':{'$in':tx_output_ids}})


def process_fork(db_chain, rpc, current_height):
    height = current_height
    # if height % 20 > 0:
    #     return

    def check_block(hash_):
        b = db_chain.block.find({'hash':hash_})
        return not not b.count()

    fork_height = -1
    while True:
        try:
            header = get_header(rpc, height)
            header = json.loads(header)
            hash = header['result']['hash']

            # if height % 10 == 9:
            #     hash = 'fuck'
            existed = check_block(hash)
            if not existed:
                fork_height = height
            else:
                if current_height == height:
                    height -= 1
                    continue
                break
            if height == 1:
                break
            height -= 1
        except Exception as e:
            logging.info('process fork exception,%s,%s' % (e, height))
            break
    if fork_height >= 0:
        logging.info('fork at %s' % fork_height)
        clear_fork(db_chain, fork_height)


def get_global_asset(db_chain):
    global global_asset
    assets = db_chain.asset_output.find({})
    for a in assets:
        global_asset[a['asset']] = int(a['asset_type'])


def init_index(db_chain):
    db_chain.block.create_index('number')
    db_chain.transaction.create_index('tx_id')
    db_chain.output.create_index('output_id')
    db_chain.input.create_index('input_id')
    db_chain.output.create_index([("hash", pymongo.ASCENDING), ("index", pymongo.ASCENDING), ('asset', pymongo.ASCENDING)]) 


def workaholic(stopped):
    db_chain = new_mongo(mongodb_host, mongodb_port, db_name)
    init_index(db_chain)
    names = db_chain.collection_names()
    logging.info(names)
    rpc = new_rpcclient(rpc_uri)

    last_height = 0
    get_global_asset(db_chain)

    def latest_addresses(db_chain_):
        addresses = db_chain_.address.find().sort('id', 1)
        addresses_ = collections.OrderedDict()
        for addr in addresses:
            addresses_[addr['address']] = addr['id']
        return addresses_

    def batch_begin(db_chain_, batch_info):
        return
        db_chain_.batch.insert_one(batch_info)

    def batch_end(db_chain_, batch_info):
        return
        db_chain_.batch.remove(batch_info)

    addresses = latest_addresses(db_chain)

    while not stopped():
        block_height = -1
        try:
            block_height, tx_id, o_id, i_id = get_last_height(db_chain)
            if last_height == block_height:
                time.sleep(1)
            block, transactions, addresses_, outs, asset_outs, inputs, new_txs = sync_block(db_chain, block_height, tx_id, o_id, i_id, addresses, rpc)
            output_line = 'block,%s, tx,%s,address,%s, output,%s, input, %s' % \
                          (block['number'], len(transactions), len(addresses_), len(outs), len(inputs))

            logging.info(output_line)

            batch_info = {'block_height': block_height, 'tx_id':tx_id, 'output_id':o_id, 'input_id':i_id}
            batch_begin(db_chain, batch_info)
            dirty_worker(db_chain, block, transactions, addresses_, outs, asset_outs, inputs, new_txs)
            batch_end(db_chain, batch_info)
        except TryException as e:
            pass
        except HeaderNotFound as e:
            logging.info('header not found except,%s' % e)
            time.sleep(24)
            continue
        except RpcConnectionException as e:
            logging.error('rpc network problem,%s' % e)
            break
        except int as e:
            logging.error('workholic exception,%s' % e)
        process_fork(db_chain, rpc, block_height)
        if block_height > 0:
            last_height = block_height

    logging.info('service begin to exit...')


def do_clear(args):
    pass


def do_check(args):
    pass


def do_fork(args):
    assert(len(args) == 1)
    db_chain = new_mongo(mongodb_host, mongodb_port, db_name)
    clear_fork(db_chain, int(args[0]))


def do_help(args):
    print(
'''Usage:python block_sync.py action [options...]
action:
    clear    clear all database
    fork
    check
    help''')

import sys

def main(argv):
    if len(argv) > 1:
        action = sys.argv[1]
        cmd_action = {'clear': do_clear, 'help': do_help, 'check': do_check, 'fork': do_fork}
        if action in cmd_action:
            cmd_action[action](argv[2:])
            return
        else:
            do_help(argv[2:])
            return

    logging.info('service begin...')
    stopped = Boolean()
    set_up_signal(stopped)
    workaholic(stopped)


if __name__ == '__main__':
    main(sys.argv)
