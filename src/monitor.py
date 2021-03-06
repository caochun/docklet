#!/usr/bin/python3

import subprocess,re,os,etcdlib,psutil,math,sys
import time,threading,json,traceback,platform

from model import db,VNode,History
from log import logger

monitor_hosts = {}
monitor_vnodes = {}

workerinfo = {}
workercinfo = {}
containerpids = []
pid2name = {}

laststopcpuval = {}
laststopruntime = {}
lastbillingtime = {}
increment = {}

class Container_Collector(threading.Thread):

    def __init__(self,test=False):
        global laststopcpuval
        global workercinfo
        threading.Thread.__init__(self)
        self.thread_stop = False
        self.interval = 2
        self.billingtime = 3600
        self.test = test
        self.cpu_last = {}
        self.cpu_quota = {}
        self.mem_quota = {}
        self.cores_num = int(subprocess.getoutput("grep processor /proc/cpuinfo | wc -l"))
        containers = self.list_container()
        for container in containers:
            if not container == '':
                try:
                    vnode = VNode.query.get(container)
                    laststopcpuval[container] = vnode.laststopcpuval
                    laststopruntime[container] = vnode.laststopruntime
                    workercinfo[container] = {}
                    workercinfo[container]['basic_info'] = {}
                    workercinfo[container]['basic_info']['billing'] = vnode.billing
                    workercinfo[container]['basic_info']['RunningTime'] = vnode.laststopruntime
                except:
                    laststopcpuval[container] = 0
                    laststopruntime[container] = 0
        return

    def list_container(self):
        output = subprocess.check_output(["sudo lxc-ls"],shell=True)
        output = output.decode('utf-8')
        containers = re.split('\s+',output)
        return containers

    def get_proc_etime(self,pid):
        fmt = subprocess.getoutput("ps -A -opid,etime | grep '^ *%d' | awk '{print $NF}'" % pid).strip()
        if fmt == '':
            return -1
        parts = fmt.split('-')
        days = int(parts[0]) if len(parts) == 2 else 0
        fmt = parts[-1]
        parts = fmt.split(':')
        hours = int(parts[0]) if len(parts) == 3 else 0
        parts = parts[len(parts)-2:]
        minutes = int(parts[0])
        seconds = int(parts[1])
        return ((days * 24 + hours) * 60 + minutes) * 60 + seconds

    def collect_containerinfo(self,container_name):
        global workerinfo
        global workercinfo
        global increment
        global lastbillingtime
        global containerpids
        global pid2name
        global laststopcpuval
        global laststopruntime
        output = subprocess.check_output("sudo lxc-info -n %s" % (container_name),shell=True)
        output = output.decode('utf-8')
        parts = re.split('\n',output)
        info = {}
        basic_info = {}
        basic_exist = 'basic_info' in workercinfo[container_name].keys()
        if basic_exist:
            basic_info = workercinfo[container_name]['basic_info']
        else:
            basic_info['RunningTime'] = 0
            basic_info['billing'] = 0
        for part in parts:
            if not part == '':
                key_val = re.split(':',part)
                key = key_val[0]
                val = key_val[1]
                info[key] = val.lstrip()
        basic_info['Name'] = info['Name']
        basic_info['State'] = info['State']
        #if basic_exist:
         #   logger.info(workercinfo[container_name]['basic_info'])
        if(info['State'] == 'STOPPED'):
            workercinfo[container_name]['basic_info'] = basic_info
            #logger.info(basic_info)
            return False
        if not info['PID'] in containerpids:
            containerpids.append(info['PID'])
            pid2name[info['PID']] = container_name
        running_time = self.get_proc_etime(int(info['PID']))
        running_time += laststopruntime[container_name]
        basic_info['PID'] = info['PID']
        basic_info['IP'] = info['IP']
        basic_info['RunningTime'] = running_time

        cpu_parts = re.split(' +',info['CPU use'])
        cpu_val = float(cpu_parts[0].strip())
        cpu_unit = cpu_parts[1].strip()
        if not container_name in self.cpu_last.keys():
            confpath = "/var/lib/lxc/%s/config"%(container_name)
            if os.path.exists(confpath):
                confile = open(confpath,'r')
                res = confile.read()
                lines = re.split('\n',res)
                for line in lines:
                    words = re.split('=',line)
                    key = words[0].strip()
                    if key == "lxc.cgroup.memory.limit_in_bytes":
                        self.mem_quota[container_name] = float(words[1].strip().strip("M"))*1000000/1024
                    elif key == "lxc.cgroup.cpu.cfs_quota_us":
                        tmp = int(words[1].strip())
                        if tmp < 0:
                            self.cpu_quota[container_name] = self.cores_num
                        else:
                            self.cpu_quota[container_name] = tmp/100000.0
                quota = {'cpu':self.cpu_quota[container_name],'memory':self.mem_quota[container_name]}
                #logger.info(quota)
                workercinfo[container_name]['quota'] = quota
            else:
                logger.error("Cant't find config file %s"%(confpath))
                return False
            self.cpu_last[container_name] = 0 
        cpu_use = {}
        lastval = 0
        try:
            lastval = laststopcpuval[container_name]
        except:
            logger.warning(traceback.format_exc())   
        cpu_val += lastval
        cpu_use['val'] = cpu_val
        cpu_use['unit'] = cpu_unit
        cpu_usedp = (float(cpu_val)-float(self.cpu_last[container_name]))/(self.cpu_quota[container_name]*self.interval*1.05)
        cpu_use['hostpercent'] = (float(cpu_val)-float(self.cpu_last[container_name]))/(self.cores_num*self.interval*1.05)
        if(cpu_usedp > 1 or cpu_usedp < 0):
            cpu_usedp = 1
        cpu_use['usedp'] = cpu_usedp
        self.cpu_last[container_name] = cpu_val;
        workercinfo[container_name]['cpu_use'] = cpu_use
        
        if container_name not in increment.keys():
            increment[container_name] = {}
            increment[container_name]['lastcputime'] = cpu_val
            increment[container_name]['memincrement'] = 0

        mem_parts = re.split(' +',info['Memory use'])
        mem_val = mem_parts[0].strip()
        mem_unit = mem_parts[1].strip()
        mem_use = {}
        mem_use['val'] = mem_val
        mem_use['unit'] = mem_unit
        if(mem_unit == "MiB"):
            mem_val = float(mem_val) * 1024
            increment[container_name]['memincrement'] += float(mem_val)
        elif (mem_unit == "GiB"):
            mem_val = float(mem_val) * 1024 * 1024
            increment[container_name]['memincrement'] += float(mem_val)*1024
        mem_usedp = float(mem_val) / self.mem_quota[container_name]
        mem_use['usedp'] = mem_usedp
        workercinfo[container_name]['mem_use'] = mem_use
        
        if not container_name in lastbillingtime.keys():
            lastbillingtime[container_name] = int(running_time/self.billingtime)
        lasttime = lastbillingtime[container_name]
        #logger.info(lasttime)
        if not int(running_time/self.billingtime) == lasttime:
            #logger.info("billing:"+str(float(cpu_val)))
            lastbillingtime[container_name] = int(running_time/self.billingtime)
            cpu_increment = float(cpu_val) - float(increment[container_name]['lastcputime'])
            #logger.info("billing:"+str(cpu_increment)+" "+str(increment[container_name]['lastcputime']))
            if cpu_increment == 0.0:
                avemem = 0
            else:
                avemem = cpu_increment*float(increment[container_name]['memincrement'])/1800.0
            increment[container_name]['lastcputime'] = cpu_val
            increment[container_name]['memincrement'] = 0
            if 'disk_use' in workercinfo[container_name].keys():
                disk_quota = workercinfo[container_name]['disk_use']['total']
            else:
                disk_quota = 0
            #logger.info("cpu_increment:"+str(cpu_increment)+" avemem:"+str(avemem)+" disk:"+str(disk_quota)+"\n")
            billing = cpu_increment/1000.0 + avemem/500000.0 + float(disk_quota)/1024.0/1024.0/2000
            basic_info['billing'] += math.ceil(billing)
            try:
                vnode = VNode.query.get(container_name)
                vnode.billing = basic_info['billing']
                db.session.commit()
            except Exception as err:
                vnode = VNode(container_name)
                vnode.billing = basic_info['billing']
                db.session.add(vnode)
                db.session.commit()
                logger.warning(err)
        workercinfo[container_name]['basic_info'] = basic_info
        #print(output)
        #print(parts)
        return True

    def run(self):
        global workercinfo
        global workerinfo
        cnt = 0
        while not self.thread_stop:
            containers = self.list_container()
            countR = 0
            conlist = []
            for container in containers:
                if not container == '':
                    conlist.append(container)
                    if not container in workercinfo.keys():
                        workercinfo[container] = {}
                    try:
                        success= self.collect_containerinfo(container)
                        if(success):
                            countR += 1
                    except Exception as err:
                        logger.warning(traceback.format_exc())
                        logger.warning(err)
            containers_num = len(containers)-1
            concnt = {}
            concnt['total'] = containers_num
            concnt['running'] = countR
            workerinfo['containers'] = concnt
            time.sleep(self.interval)
            if cnt == 0:
                workerinfo['containerslist'] = conlist
            cnt = (cnt+1)%5
            if self.test:
                break
        return

    def stop(self):
        self.thread_stop = True


class Collector(threading.Thread):

    def __init__(self,test=False):
        global workerinfo
        threading.Thread.__init__(self)
        self.thread_stop = False
        self.interval = 1
        self.test=test
        workerinfo['concpupercent'] = {}
        return

    def collect_meminfo(self):
        meminfo = psutil.virtual_memory()
        memdict = {}
        memdict['total'] = meminfo.total/1024
        memdict['used'] = meminfo.used/1024
        memdict['free'] = meminfo.free/1024
        memdict['buffers'] = meminfo.buffers/1024
        memdict['cached'] = meminfo.cached/1024
        memdict['percent'] = meminfo.percent
        #print(output)
        #print(memparts)
        return memdict

    def collect_cpuinfo(self):
        cpuinfo = psutil.cpu_times_percent(interval=1,percpu=False)
        cpuset = {}
        cpuset['user'] = cpuinfo.user
        cpuset['system'] = cpuinfo.system
        cpuset['idle'] = cpuinfo.idle
        cpuset['iowait'] = cpuinfo.iowait
        output = subprocess.check_output(["cat /proc/cpuinfo"],shell=True)
        output = output.decode('utf-8')
        parts = output.split('\n')
        info = []
        idx = -1
        for part in parts:
            if not part == '':
                key_val = re.split(':',part)
                key = key_val[0].rstrip()
                if key == 'processor':
                    info.append({})
                    idx += 1
                val = key_val[1].lstrip()
                if key=='processor' or key=='model name' or key=='core id' or key=='cpu MHz' or key=='cache size' or key=='physical id':
                    info[idx][key] = val
        return [cpuset, info]

    def collect_diskinfo(self):
        global workercinfo
        parts = psutil.disk_partitions()
        setval = []
        devices = {}
        for part in parts:
            if not part.device in devices:
                devices[part.device] = 1
                diskval = {}
                diskval['device'] = part.device
                diskval['mountpoint'] = part.mountpoint
                try:
                    usage = psutil.disk_usage(part.mountpoint)
                    diskval['total'] = usage.total
                    diskval['used'] = usage.used
                    diskval['free'] = usage.free
                    diskval['percent'] = usage.percent
                    if(part.mountpoint.startswith('/opt/docklet/local/volume')):
                        names = re.split('/',part.mountpoint)
                        container = names[len(names)-1]
                        if not container in workercinfo.keys():
                            workercinfo[container] = {}
                        workercinfo[container]['disk_use'] = diskval 
                    setval.append(diskval)
                except Exception as err:
                    logger.warning(traceback.format_exc())
                    logger.warning(err)
        #print(output)
        #print(diskparts)
        return setval

    def collect_osinfo(self):
        uname = platform.uname()
        osinfo = {}
        osinfo['platform'] = platform.platform()
        osinfo['system'] = uname.system
        osinfo['node'] = uname.node
        osinfo['release'] = uname.release
        osinfo['version'] = uname.version
        osinfo['machine'] = uname.machine
        osinfo['processor'] = uname.processor
        return osinfo

    def collect_concpuinfo(self):
        global workerinfo
        global containerpids
        global pid2name
        l = len(containerpids)
        if l == 0:
            return
        cmd = "sudo top -bn 1"
        for pid in containerpids:
            cmd = cmd + " -p " + pid
        #child = subprocess.Popen(cmd,shell=True,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
        #[stdout,errout] = child.communicate()
        #logger.info(errout)
        #logger.info(stdout)
        output = ""
        output = subprocess.check_output(cmd,shell=True)
        output = output.decode('utf-8')
        parts = re.split("\n",output)
        concpupercent = workerinfo['concpupercent']
        for line in parts[7:]:
            if line == "":
                continue
            info = re.split(" +",line)
            pid = info[1].strip()
            cpupercent = float(info[9].strip())
            name = pid2name[pid]
            concpupercent[name] = cpupercent

    def run(self):
        global workerinfo
        workerinfo['osinfo'] = self.collect_osinfo()
        while not self.thread_stop:
            workerinfo['meminfo'] = self.collect_meminfo()
            [cpuinfo,cpuconfig] = self.collect_cpuinfo()
            #self.collect_concpuinfo()
            workerinfo['cpuinfo'] = cpuinfo
            workerinfo['cpuconfig'] = cpuconfig
            workerinfo['diskinfo'] = self.collect_diskinfo()
            workerinfo['running'] = True
            #time.sleep(self.interval)
            if self.test:
                break
            #   print(self.etcdser.getkey('/meminfo/total'))
        return

    def stop(self):
        self.thread_stop = True

def workerFetchInfo():
    global workerinfo
    global workercinfo
    return str([workerinfo, workercinfo])

def get_owner(container_name):
    names = container_name.split('-')
    return names[0]

class Master_Collector(threading.Thread):

    def __init__(self,nodemgr):
        threading.Thread.__init__(self)
        self.thread_stop = False
        self.nodemgr = nodemgr
        return

    def run(self):
        global monitor_hosts
        global monitor_vnodes
        while not self.thread_stop:
            for worker in monitor_hosts.keys():
                monitor_hosts[worker]['running'] = False
            workers = self.nodemgr.get_rpcs()
            for worker in workers:
                try:
                    ip = self.nodemgr.rpc_to_ip(worker)
                    info = list(eval(worker.workerFetchInfo()))
                    #logger.info(info[0])
                    monitor_hosts[ip] = info[0]
                    for container in info[1].keys():
                        owner = get_owner(container)
                        if not owner in monitor_vnodes.keys():
                            monitor_vnodes[owner] = {}
                        monitor_vnodes[owner][container] = info[1][container]
                except Exception as err:
                    logger.warning(traceback.format_exc())
                    logger.warning(err)
            time.sleep(2)
            #logger.info(History.query.all())
            #logger.info(VNode.query.all())
        return

    def stop(self):
        self.thread_stop = True
        return

class Container_Fetcher:
    def __init__(self,container_name):
        self.owner = get_owner(container_name)
        self.con_id = container_name
        return

    def get_cpu_use(self):
        global monitor_vnodes
        try:
            res = monitor_vnodes[self.owner][self.con_id]['cpu_use']
            res['quota'] = monitor_vnodes[self.owner][self.con_id]['quota']
        except Exception as err:
            logger.warning(traceback.format_exc())
            logger.warning(err)
            res = {}
        return res

    def get_mem_use(self):
        global monitor_vnodes
        try:
            res = monitor_vnodes[self.owner][self.con_id]['mem_use']
            res['quota'] = monitor_vnodes[self.owner][self.con_id]['quota']
        except Exception as err:
            logger.warning(traceback.format_exc())
            logger.warning(err)
            res = {}
        return res

    def get_disk_use(self):
        global monitor_vnodes
        try:
            res = monitor_vnodes[self.owner][self.con_id]['disk_use']
        except Exception as err:
            logger.warning(traceback.format_exc())
            logger.warning(err)
            res = {}
        return res

    def get_basic_info(self):
        global monitor_vnodes
        try:
            res = monitor_vnodes[self.owner][self.con_id]['basic_info']
        except Exception as err:
            logger.warning(traceback.format_exc())
            logger.warning(err)
            res = {}
        return res

class Fetcher:

    def __init__(self,host):
        global monitor_hosts
        self.info = monitor_hosts[host]
        return

    #def get_clcnt(self):
    #   return DockletMonitor.clcnt

    #def get_nodecnt(self):
    #   return DockletMonitor.nodecnt

    #def get_meminfo(self):
    #   return self.get_meminfo_('172.31.0.1')

    def get_meminfo(self):
        try:
            res = self.info['meminfo']
        except Exception as err:
            logger.warning(traceback.format_exc())
            logger.warning(err)
            res = {}
        return res

    def get_cpuinfo(self):
        try:
            res = self.info['cpuinfo']
        except Exception as err:
            logger.warning(traceback.format_exc())
            logger.warning(err)
            res = {}
        return res

    def get_cpuconfig(self):
        try:
            res = self.info['cpuconfig']
        except Exception as err:
            logger.warning(traceback.format_exc())
            logger.warning(err)
            res = {}
        return res

    def get_diskinfo(self):
        try:
            res = self.info['diskinfo']
        except Exception as err:
            logger.warning(traceback.format_exc())
            logger.warning(err)
            res = {}
        return res

    def get_osinfo(self):
        try:
            res = self.info['osinfo']
        except Exception as err:
            logger.warning(traceback.format_exc())
            logger.warning(err)
            res = {}
        return res

    def get_concpuinfo(self):
        try:
            res = self.info['concpupercent']
        except Exception as err:
            logger.warning(traceback.format_exc())
            logger.warning(err)
            res = {}
        return res

    def get_containers(self):
        try:
            res = self.info['containers']
        except Exception as err:
            logger.warning(traceback.format_exc())
            logger.warning(err)
            res = {}
        return res

    def get_status(self):
        try:
            isexist = self.info['running']
        except Exception as err:
            logger.warning(traceback.format_exc())
            logger.warning(err)
            isexist = False
        if(isexist):
            return 'RUNNING'
        else:
            return 'STOPPED'

    def get_containerslist(self):
        try:
            res = self.info['containerslist']
        except Exception as err:
            logger.warning(traceback.format_exc())
            logger.warning(err)
            res = {}
        return res

class History_Manager:
    
    def __init__(self):
        try:
            VNode.query.all()
            History.query.all()
        except:
            db.create_all(bind='__all__')

    def getAll(self):
        return History.query.all()
    
    def log(self,vnode_name,action):
        global workercinfo
        global laststopcpuval
        res = VNode.query.filter_by(name=vnode_name).first()
        if res is None:
            vnode = VNode(vnode_name)
            vnode.histories = []
            db.session.add(vnode)
            db.session.commit()
        vnode = VNode.query.get(vnode_name)
        billing = 0
        cputime = 0
        runtime = 0
        try:
            owner = get_owner(vnode_name)
            billing = int(workercinfo[vnode_name]['basic_info']['billing'])
            cputime = float(workercinfo[vnode_name]['cpu_use']['val'])
            runtime = float(workercinfo[vnode_name]['basic_info']['RunningTime'])
        except Exception as err:
            #print(traceback.format_exc())
            billing = 0
            cputime = 0.0
            runtime = 0
        history = History(action,runtime,cputime,billing)
        vnode.histories.append(history)
        if action == 'Stop' or action == 'Create':
            laststopcpuval[vnode_name] = cputime
            vnode.laststopcpuval = cputime
            laststopruntime[vnode_name] = runtime
            vnode.laststopruntime = runtime
        db.session.add(history)
        db.session.commit()

    def getHistory(self,vnode_name):
        vnode = VNode.query.filter_by(name=vnode_name).first()
        if vnode is None:
            return []
        else:
            res = History.query.filter_by(vnode=vnode_name).all()
            return list(eval(str(res)))

    def getCreatedVNodes(self,owner):
        vnodes = VNode.query.filter(VNode.name.startswith(owner)).all()
        res = []
        for vnode in vnodes:
            res.append(vnode.name)
        return res
