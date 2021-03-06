# Copyright (c) Ran Dugal 2014
#
# This file is part of dust.
#
# Licensed under the GNU Affero General Public License v3, which is available at
# http://www.gnu.org/licenses/agpl-3.0.html
# 
# This program is distributed in the hope that it will be useful, but WITHOUT 
# ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS
# FOR A PARTICULAR PURPOSE. See the GNU Affero GPL for more details.
#

''' EC2 cluster and node objects '''
import logging
import os, sys
from copy import deepcopy, copy
import boto, boto.ec2

from dustcluster.util import setup_logger
logger = setup_logger( __name__ )

class EC2Cloud(object):
    '''
    provides a connection to EC2 and generates a list of Node objects 
    '''

    def __init__(self, name='', key='', region="", image="", username="", keyfile="", creds_map={}):

        if not region:
            region = 'eu-west-1'

        self._connection = None
        # cluster name
        self.name   = name
        self.key    = key
        self.region = region
        self.image  = image
        self.username = username
        self.keyfile = keyfile
        self.creds_map = creds_map


    def connect(self):

        if not self.region:
            raise Exception('No region specified. Will not connect.')
  
        conn = boto.ec2.connect_to_region(self.region,
                                            aws_access_key_id=self.creds_map['aws_access_key_id'], 
                                            aws_secret_access_key=self.creds_map['aws_secret_access_key'], 
                                            )

        if not conn:
            raise Exception("Invalid region [%s]" % self.region)

        logger.debug('Connected, boto version: %s' % conn.APIVersion)
        return conn

    def conn(self):

        if not self._connection:
            self._connection=self.connect()

        return self._connection


    def refresh(self):
        ''' get nodes/reservations from cloud '''

        logger.debug('hydrating from all cloud nodes')

        vms = self._get_instances()

        all_nodes = []
        for vm in vms:
            node = EC2Node(username=self.username, cloud=self)
            node.hydrate(vm)
            all_nodes.append(node)
        return all_nodes

    def _get_instances(self, iids=None):
        ret = []
        reservations = self.conn().get_all_reservations(instance_ids=iids)
        for r in reservations:
            for i in r.instances:
                ret.append(i)
        return ret

    def create_absent_node(self, nodename, **kwargs):
        node = EC2Node(nodename=nodename, **kwargs)
        node.cloud = self
        return node

    def create_keypair(self, keyname, keydir):
        '''  create a keypair and save it keydir/keyname.pem '''

        os.makedirs(keydir)

        keypath = os.path.join(keydir, "%s.pem" % keyname)
        if os.path.exists(keypath):
            logger.info('Found key pair locally, not doing anything. key=[%s] keypath=[%s]' % (keyname, keypath))
            return keyname, keypath 

        # check is the keys exists in the cloud
        keypairs = self.conn().get_all_key_pairs()
        for keypair in keypairs:
            if keypair.name == keyname:
                errstr = "They key %s has exists on this account already." % keyname
                logger.info('Cloud keys : %s' % str(keypairs)) 
                raise Exception(errstr)

        # create it
        key = self.conn().create_key_pair(keyname)
        if key:
            key.save(keydir)
        else:
            raise Exception('Error creating key')

        return keyname, keypath

class EC2Node(object):
    '''
    describe and control EC2 nodes within an EC2 cloud
    '''

    def __init__(self, key="", keyfile="", nodename="", instance_type="", image="",  username='', vm=None, cloud=None):

        self._key = key
        self._keyfile    = keyfile
        self._name      = nodename
        self._instance_type = instance_type
        self._image     = image
        self._username = username
        self._vm        = None
        self.cloud      = cloud

        self._hydrated = False

        # for starting new nodes
        self._clustername = None

        self.friendly_names = { 
                                'image'    : 'image_id', 
                                'dns_name' : 'public_dns_name', 
                                'type'     : 'instance_type',
                                'key'      : 'key_name',
                                'vpc'      : 'vpc_id',
                                'ip'       : 'ip_address'
                               }

        self.extended_fields = [ 'dns_name', 'image', 'tags', 'key', 'launch_time', 'vpc', 'groups']

        self.all_fields = ['ami_launch_index', 'architecture', 'block_device_mapping', 'client_token',  
                    'dns_name', 'ebs_optimized', 'group_name', 'groups', 'hypervisor', 'id', 'image_id', 'instance_profile', 
                    'instance_type', 'interfaces', 'ip_address', 'kernel', 'key_name', 'launch_time', 
                    'monitored', 'monitoring_state', 'persistent', 'placement', 'placement_group', 
                     'placement_tenancy', 'platform', 'previous_state', 'previous_state_code', 'private_dns_name', 'private_ip_address', 
                    'product_codes', 'public_dns_name', 'ramdisk', 'reason', 'reboot', 'region', 'requester_id', 
                    'root_device_name', 'root_device_type', 'spot_instance_request_id', 'state', 'state_code', 'state_reason', 
                    'subnet_id', 'tags', 'virtualization_type', 'vpc_id']

    def __repr__(self):
        data = self.disp_data()
        return ",".join(str(datum) for datum in data)

    def hydrate(self, vm):
        ''' populate template node state from the cloud reservation ''' 
        self._name      = ""
        self._image     = vm.image_id
        self._instance_type     = vm.instance_type
        self._vm = vm
        self._hydrated = True

    @property
    def hydrated(self):
        return self._hydrated

    @property
    def vm(self):
        return self._vm

    @vm.setter
    def vm(self, value):
        self._vm = value

    @property
    def name(self):
        return self._name

    @name.setter
    def name(self, value):
        self._name = value

    @property
    def cluster(self):
        return self._clustername

    @cluster.setter
    def cluster(self, value):
        self._clustername = value

    @property
    def username(self):
        return self._username

    @username.setter
    def username(self, value):
        self._username = value

    @property
    def keyfile(self):
        return self._keyfile
        
    @keyfile.setter
    def keyfile(self, value):
        self._keyfile = value

    @property
    def key(self):
        if self._vm:
            return self._vm.key_name
        else:
            return self._key

    @key.setter
    def key(self, value):
        self._key = value


    def start(self):

        #if not self.cluster.get_keyfile_for_key(self.key):
        #    raise Exception("No key specified, not starting nodes.")

        vm = self._vm
        if vm:
            if vm.state == 'running' or vm.state == 'pending':
                logger.info( "Nothing to do for node [%s]" % self._name )
                return

            if vm.state == 'stopped':
                logger.info( 'restarting node %s : %s' % (self._name, self) )
                self.cloud.conn().start_instances(instance_ids=[vm.id])
                return

        logger.info( 'launching new node name=[%s] image=[%s] instance=[%s]'
                        % (self._name, self._image, self._instance_type) )

        res = self.cloud.conn().run_instances(self._image, key_name=self._key, instance_type=self._instance_type)

    def stop(self):

        vm = self._vm
        if vm:
            if vm.state == 'stopped':
                return 
            else:
                logger.info('stopping %s' % self._name)
                self.cloud.conn().stop_instances(instance_ids = [vm.id])
        else:
            logger.error('no vm that matches node defination for %s' %  self._name)

    def terminate(self):

        if self._vm:
            tags = self._vm.tags
            newname = ''
            if tags and tags.get('name'):
                newname = tags['name'] + '_terminated'
                self._vm.add_tag('name', newname)

            instance_ids = [self._vm.id]

            logger.info('terminating %s id=[%s]' % (self._name, self._vm.id))

            self.cloud.conn().stop_instances( instance_ids = instance_ids )
            self.cloud.conn().terminate_instances( instance_ids = instance_ids )


    def disp_headers(self):
        headers = ["Name", "Type", "State", "ID",  "IP", "int_IP"]
        fmt =     ["%-12s",  "%-12s",  "%-12s",  "%-10s", "%-15s", "%-15s"]
        return headers, fmt


    def disp_data(self):

        vals = [self._name, self._instance_type]

        if self._vm:
            vm = self._vm
            vmdata = [vm.state, vm.id, vm.ip_address, vm.private_ip_address]
            vals += vmdata
        else:
            startColorRed = "\033[0;31;40m"
            endColor      = "\033[0m"
            vals += ['%sabsent%s' % (startColorRed, endColor), '', '', '']

        return vals


    def get(self, prop_name):
        ''' return property from the underlying instance '''

        if prop_name == 'name':
            val = getattr(self, 'name')
            return val

        if prop_name in self.friendly_names:
            prop_name = self.friendly_names[prop_name]

        if not self._vm:
            return ""

        if prop_name == 'groups':
            return ",".join(str(grp.name) for grp in self._vm.groups)
        else:
            return getattr(self._vm, prop_name)


    def extended_data(self):
        # updated here for showex command error
        ret = {}


        for field in self.extended_fields:
            val = self.get(field)

            if field == 'tags':
                val =  ",".join( '%s=%s' % (k,v) for k,v in self._vm.tags.items())

            if val:
                ret[field] = val

        return ret


    def all_data(self):
        # updated here for showex command error
        ret = {}
    
        for field in self.all_fields:
            val = self.get(field)

            if field == 'tags':
                val =  ",".join( '%s=%s' % (k,v) for k,v in self._vm.tags.items())

            if val:
                ret[field] = val

        return ret

