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

''' dust command for getting and putting files from/to a set of nodes '''

import glob

# export commands

commands = ['put', 'get']

def put(cmdline, cluster, logger):
    '''
    put tgt src [dest] - upload src file to a set of target nodes

    Notes:
    src can have wildcards

    Examples:
    put worker* /opt/data/data.txt  # uploads data.txt to home dir
    put worker* /opt/data/data.txt /opt/data/data.txt
    put worker* /opt/data/*.txt     # wildcards work
    '''
    target = cmdline.split()[0]

    target_nodes = cluster.running_nodes_from_target(target)
    if not target_nodes:
        return

    args = cmdline[len(target):].strip()

    arrargs = args.split()
    
    srcfile = None
    destfile = None

    if len(arrargs) > 0:
        srcfile = arrargs[0]

    if len(arrargs) > 1:
        destfile = arrargs[1]

    for node in target_nodes:
        for fname in glob.iglob(srcfile): 
            cluster.lineterm.put(cluster.cloud.keyfile, node, fname, destfile)


def get(cmdline, cluster, logger):
    '''
    get tgt remotefile [localdir] - download remotefile from a set of nodes to [localdir] or cwd as remotefile.nodename

    Notes:
    remotefile can be a wildcard 
    
    Example:
    get worker* /opt/output/*.txt        # download to cwd
    get worker* /opt/output/*.txt /tmp   # download to /tmp
    '''
    target = cmdline.split()[0]

    target_nodes = cluster.running_nodes_from_target(target)
    if not target_nodes:
        return

    args = cmdline[len(target):].strip()

    arrargs = args.split()

    remotefile = None
    localdir = None

    remotefile = arrargs[0]

    if len(arrargs) > 1:
        localdir = arrargs[1]

    for node in target_nodes:
        for fname in glob.iglob(remotefile): 
            cluster.lineterm.get(cluster.cloud.keyfile, node, fname, localdir)
