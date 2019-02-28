from vmnet.testcase import BaseNetworkTestCase
import unittest, time, random, vmnet, cilantro_ee
from os.path import join, dirname
from cilantro_ee.utils.test.mp_test_case import vmnet_test


LOG_LEVEL = 0

def wrap_func(fn, *args, **kwargs):
    def wrapper():
        return fn(*args, **kwargs)
    return wrapper

def run_mn(slot_num):
    from cilantro_ee.logger import get_logger, overwrite_logger_level
    from cilantro_ee.nodes import NodeFactory
    from cilantro_ee.constants.testnet import TESTNET_MASTERNODES

    import os
    import logging

    # overwrite_logger_level(logging.WARNING)
    overwrite_logger_level(15)

    mn_info = TESTNET_MASTERNODES[slot_num]
    mn_info['ip'] = os.getenv('HOST_IP')

    NodeFactory.run_masternode(signing_key=mn_info['sk'], ip=mn_info['ip'], reset_db=True)


def trigger_mn_wr(blocks):
    from cilantro_ee.logger import get_logger, overwrite_logger_level
    from cilantro_ee.utils.test import God
    overwrite_logger_level(15)
    God.dump_it(volume=volume, delay=delay)


class MasterStore(BaseNetworkTestCase):

    BLOCKS = 1000
    config_file = join(dirname(cilantro_ee.__path__[0]), 'vmnet_configs', 'cilantro_ee-mn.json')

    @vmnet_test(run_webui=True)
    def mn_store(self):

        # Bootstrap TESTNET_MASTERNODES
        self.execute_python('masternode', run_mn, profiling=None)

        self.execute_python('mgmt', wrap_func(trigger_mn_wr, blocks=self.BLOCKS))

        input("Enter any key to terminate")

if __name__ == '__main__':
    unittest.main()