# Copyright (c) 2014-2019, iocage
# All rights reserved.
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted providing that the following conditions
# are met:
# 1. Redistributions of source code must retain the above copyright
#    notice, this list of conditions and the following disclaimer.
# 2. Redistributions in binary form must reproduce the above copyright
#    notice, this list of conditions and the following disclaimer in the
#    documentation and/or other materials provided with the distribution.
#
# THIS SOFTWARE IS PROVIDED BY THE AUTHOR ``AS IS'' AND ANY EXPRESS OR
# IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE IMPLIED
# WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE
# ARE DISCLAIMED.  IN NO EVENT SHALL THE AUTHOR BE LIABLE FOR ANY
# DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL
# DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS
# OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION)
# HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT,
# STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING
# IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE
# POSSIBILITY OF SUCH DAMAGE.
"""This is responsible for starting jails."""
import datetime
import hashlib
import os
import re
import shutil
import json
import subprocess as su
import netifaces

import iocage_lib.ioc_common
import iocage_lib.ioc_exec
import iocage_lib.ioc_json
import iocage_lib.ioc_list
import iocage_lib.ioc_stop
import iocage_lib.ioc_exceptions as ioc_exceptions


class IOCStart(object):

    """
    Starts jails, the network stack for the jail and generates a resolv file

    for them. It also finds any scripts the user supplies for exec_*
    """

    def __init__(
        self, uuid, path, silent=False, callback=None,
        is_depend=False, unit_test=False, suppress_exception=False
    ):
        self.uuid = uuid.replace(".", "_")
        self.path = path
        self.callback = callback
        self.silent = silent
        self.is_depend = is_depend
        self.unit_test = unit_test

        if not self.unit_test:
            self.conf = iocage_lib.ioc_json.IOCJson(path).json_get_value('all')
            self.pool = iocage_lib.ioc_json.IOCJson(" ").json_get_value("pool")
            self.iocroot = iocage_lib.ioc_json.IOCJson(
                self.pool).json_get_value("iocroot")
            self.get = iocage_lib.ioc_json.IOCJson(self.path,
                                                   silent=True).json_get_value
            self.set = iocage_lib.ioc_json.IOCJson(self.path,
                                                   silent=True).json_set_value

            self.exec_fib = self.conf["exec_fib"]
            try:
                self.__start_jail__()
            except (Exception, SystemExit) as e:
                if not suppress_exception:
                    raise e

    def __start_jail__(self):
        """
        Takes a UUID, and the user supplied name of a jail, the path and the
        configuration location. It then supplies the jail utility with that
        information in a format it can parse.

        start_jail also checks if the jail is already running, if the
        user wished for procfs or linprocfs to be mounted, and the user's
        specified data that is meant to populate resolv.conf
        will be copied into the jail.
        """
        status, _ = iocage_lib.ioc_list.IOCList().list_get_jid(self.uuid)
        userland_version = float(os.uname()[2].partition("-")[0])

        # If the jail is not running, let's do this thing.

        if status:
            msg = f"{self.uuid} is already running!"
            iocage_lib.ioc_common.logit({
                "level": "EXCEPTION",
                "message": msg,
                "force_raise": self.is_depend
            }, _callback=self.callback,
                silent=self.silent,
                exception=ioc_exceptions.JailRunning)

        if self.conf["hostid_strict_check"] == "on":
            with open("/etc/hostid", "r") as _file:
                hostid = _file.read().strip()
            if self.conf["hostid"] != hostid:
                iocage_lib.ioc_common.logit({
                    "level": "ERROR",
                    "message": f"{self.uuid} hostid is not matching and"
                               " 'hostid_strict_check' is on!"
                               " - Not starting jail"
                }, _callback=self.callback, silent=self.silent)
                return

        mount_procfs = self.conf["mount_procfs"]
        host_domainname = self.conf["host_domainname"]
        host_hostname = self.conf["host_hostname"]
        securelevel = self.conf["securelevel"]
        enforce_statfs = self.conf["enforce_statfs"]
        children_max = self.conf["children_max"]
        allow_set_hostname = self.conf["allow_set_hostname"]
        allow_sysvipc = self.conf["allow_sysvipc"]
        allow_raw_sockets = self.conf["allow_raw_sockets"]
        allow_chflags = self.conf["allow_chflags"]
        allow_mlock = self.conf["allow_mlock"]
        allow_mount = self.conf["allow_mount"]
        allow_mount_devfs = self.conf["allow_mount_devfs"]
        allow_mount_fusefs = self.conf["allow_mount_fusefs"]
        allow_mount_nullfs = self.conf["allow_mount_nullfs"]
        allow_mount_procfs = self.conf["allow_mount_procfs"]
        allow_mount_tmpfs = self.conf["allow_mount_tmpfs"]
        allow_mount_zfs = self.conf["allow_mount_zfs"]
        allow_quotas = self.conf["allow_quotas"]
        allow_socket_af = self.conf["allow_socket_af"]
        allow_vmm = self.conf["allow_vmm"]
        devfs_ruleset = iocage_lib.ioc_common.generate_devfs_ruleset(self.conf)
        exec_prestart = self.conf["exec_prestart"]
        exec_poststart = self.conf["exec_poststart"]
        exec_clean = self.conf["exec_clean"]
        exec_timeout = self.conf["exec_timeout"]
        stop_timeout = self.conf["stop_timeout"]
        mount_devfs = self.conf["mount_devfs"]
        mount_fdescfs = self.conf["mount_fdescfs"]
        sysvmsg = self.conf["sysvmsg"]
        sysvsem = self.conf["sysvsem"]
        sysvshm = self.conf["sysvshm"]
        bpf = self.conf["bpf"]
        dhcp = self.conf["dhcp"]
        rtsold = self.conf['rtsold']
        wants_dhcp = True if dhcp == 'on' or 'DHCP' in self.conf[
            'ip4_addr'].upper() else False
        vnet_interfaces = self.conf["vnet_interfaces"]
        ip6_addr = self.conf["ip6_addr"]
        prop_missing = False
        prop_missing_msgs = []

        if wants_dhcp:
            if bpf != "yes":
                prop_missing_msgs.append(
                    f"{self.uuid}: dhcp requires bpf=yes!"
                )
                prop_missing = True
            elif self.conf["vnet"] != "on":
                # We are already setting a vnet variable below.
                prop_missing_msgs.append(
                    f"{self.uuid}: dhcp requires vnet=on!"
                )
                prop_missing = True

        if 'accept_rtadv' in ip6_addr and self.conf['vnet'] != 'on':
            prop_missing_msgs.append(
                f'{self.uuid}: accept_rtadv requires vnet=on!'
            )
            prop_missing = True

        if bpf == 'yes' and self.conf['vnet'] != 'on':
            prop_missing_msgs.append(f'{self.uuid}: bpf requires vnet=on!')
            prop_missing = True

        if prop_missing:
            iocage_lib.ioc_common.logit({
                "level": "EXCEPTION",
                "message": '\n'.join(prop_missing_msgs)
            }, _callback=self.callback,
                silent=self.silent)

        if wants_dhcp:
            self.__check_dhcp__()

        if rtsold == 'on':
            self.__check_rtsold__()

        if mount_procfs == "1":
            su.Popen(
                [
                    'mount', '-t', 'procfs', 'proc', f'{self.path}/root/proc'
                ]
            ).communicate()

        try:
            mount_linprocfs = self.conf["mount_linprocfs"]

            if mount_linprocfs == "1":
                if not os.path.isdir(f"{self.path}/root/compat/linux/proc"):
                    os.makedirs(f"{self.path}/root/compat/linux/proc", 0o755)
                su.Popen(
                    [
                        'mount', '-t', 'linprocfs', 'linproc',
                        f'{self.path}/root/compat/linux/proc'
                    ]
                ).communicate()
        except Exception:
            pass

        if self.conf["jail_zfs"] == "on":
            allow_mount = "1"
            enforce_statfs = enforce_statfs if enforce_statfs != "2" \
                else "1"
            allow_mount_zfs = "1"

            for jdataset in self.conf["jail_zfs_dataset"].split():
                jdataset = jdataset.strip()

                try:
                    su.check_call(["zfs", "get", "-H", "creation",
                                   f"{self.pool}/{jdataset}"],
                                  stdout=su.PIPE, stderr=su.PIPE)
                except su.CalledProcessError:
                    iocage_lib.ioc_common.checkoutput(
                        ["zfs", "create", "-o",
                         "compression=lz4", "-o",
                         "mountpoint=none",
                         f"{self.pool}/{jdataset}"],
                        stderr=su.STDOUT)

                try:
                    iocage_lib.ioc_common.checkoutput(
                        ["zfs", "set", "jailed=on",
                         f"{self.pool}/{jdataset}"],
                        stderr=su.STDOUT)
                except su.CalledProcessError as err:
                    raise RuntimeError(
                        f"{err.output.decode('utf-8').rstrip()}")

        # FreeBSD 9.3 and under do not support this.

        if userland_version <= 9.3:
            tmpfs = ""
            fdescfs = ""
        else:
            tmpfs = f"allow.mount.tmpfs={allow_mount_tmpfs}"
            fdescfs = f"mount.fdescfs={mount_fdescfs}"

        # FreeBSD 10.3 and under do not support this.

        if userland_version <= 10.3:
            _sysvmsg = ""
            _sysvsem = ""
            _sysvshm = ""
        else:
            _sysvmsg = f"sysvmsg={sysvmsg}"
            _sysvsem = f"sysvsem={sysvsem}"
            _sysvshm = f"sysvshm={sysvshm}"

        # FreeBSD before 12.0 does not support this.

        if userland_version < 12.0:
            _allow_mlock = ""
            _allow_mount_fusefs = ""
            _allow_vmm = ""
        else:
            _allow_mlock = f"allow.mlock={allow_mlock}"
            _allow_mount_fusefs = f"allow.mount.fusefs={allow_mount_fusefs}"
            _allow_vmm = f"allow.vmm={allow_vmm}"

        if self.conf['vnet'] == 'off':
            ip4_addr = self.conf['ip4_addr']
            ip4_saddrsel = self.conf['ip4_saddrsel']
            ip4 = self.conf['ip4']
            ip6_saddrsel = self.conf['ip6_saddrsel']
            ip6 = self.conf['ip6']
            net = []

            if ip4_addr != 'none':
                ip4_addr = self.check_aliases(ip4_addr, '4')

                net.append(f'ip4.addr={ip4_addr}')

            if ip6_addr != 'none':
                ip6_addr = self.check_aliases(ip6_addr, '6')

                net.append(f'ip6.addr={ip6_addr}')

            net += [
                f'ip4.saddrsel={ip4_saddrsel}',
                f'ip4={ip4}',
                f'ip6.saddrsel={ip6_saddrsel}',
                f'ip6={ip6}'
            ]

            vnet = False
        else:
            net = ["vnet"]

            if vnet_interfaces != "none":
                for vnet_int in vnet_interfaces.split():
                    net += [f"vnet.interface={vnet_int}"]
            else:
                vnet_interfaces = ""

            vnet = True

        msg = f"* Starting {self.uuid}"
        iocage_lib.ioc_common.logit({
            "level": "INFO",
            "message": msg
        },
            _callback=self.callback,
            silent=self.silent)

        if wants_dhcp and self.conf['type'] != 'pluginv2' \
                and self.conf['devfs_ruleset'] != '4':
            iocage_lib.ioc_common.logit({
                "level": "WARNING",
                "message": f"  {self.uuid} is not using the devfs_ruleset"
                           f" of 4, not generating a ruleset for the jail,"
                           " DHCP may not work."
            },
                _callback=self.callback,
                silent=self.silent)

        if self.conf["type"] == "pluginv2" and os.path.isfile(
                f"{self.path}/{self.uuid.rsplit('_', 1)[0]}.json"):
            with open(f"{self.path}/{self.uuid.rsplit('_', 1)[0]}.json",
                      "r") as f:
                devfs_json = json.load(f)
                if "devfs_ruleset" in devfs_json:
                    plugin_name = self.uuid.rsplit('_', 1)[0]
                    plugin_devfs = devfs_json[
                        "devfs_ruleset"][f"plugin_{plugin_name}"]
                    plugin_devfs_paths = plugin_devfs['paths']

                    plugin_devfs_includes = None if 'includes' not in \
                        plugin_devfs else plugin_devfs['includes']

                    devfs_ruleset = \
                        iocage_lib.ioc_common.generate_devfs_ruleset(
                            self.conf,
                            paths=plugin_devfs_paths,
                            includes=plugin_devfs_includes
                        )

        parameters = [
            fdescfs, _allow_mlock, tmpfs,
            _allow_mount_fusefs, _allow_vmm,
            f"allow.set_hostname={allow_set_hostname}",
            f"mount.devfs={mount_devfs}",
            f"allow.raw_sockets={allow_raw_sockets}",
            f"allow.sysvipc={allow_sysvipc}",
            f"allow.quotas={allow_quotas}",
            f"allow.socket_af={allow_socket_af}",
            f"allow.chflags={allow_chflags}",
            f"allow.mount={allow_mount}",
            f"allow.mount.devfs={allow_mount_devfs}",
            f"allow.mount.nullfs={allow_mount_nullfs}",
            f"allow.mount.procfs={allow_mount_procfs}",
            f"allow.mount.zfs={allow_mount_zfs}"
        ]

        start_parameters = [
            x for x in net
            + [x for x in parameters if '1' in x]
            + [
                f'name=ioc-{self.uuid}',
                _sysvmsg,
                _sysvsem,
                _sysvshm,
                f'host.domainname={host_domainname}',
                f'host.hostname={host_hostname}',
                f'path={self.path}/root',
                f'securelevel={securelevel}',
                f'host.hostuuid={self.uuid}',
                f'devfs_ruleset={devfs_ruleset}',
                f'enforce_statfs={enforce_statfs}',
                f'children.max={children_max}',
                f'exec.prestart={exec_prestart}',
                f'exec.clean={exec_clean}',
                f'exec.timeout={exec_timeout}',
                f'stop.timeout={stop_timeout}',
                f'mount.fstab={self.path}/fstab',
                'allow.dying',
                f'exec.consolelog={self.iocroot}/log/ioc-'
                f'{self.uuid}-console.log',
                'persist'
            ] if x != '']

        # Write the config out to a file. We'll be starting the jail using this
        # config and it is required for stopping the jail too.
        jail = iocage_lib.ioc_json.JailRuntimeConfiguration(
            self.uuid, start_parameters
        )
        jail.sync_changes()

        start_cmd = ["jail", "-f", f"/var/run/jail.ioc-{self.uuid}.conf", "-c"]

        start_env = {
            **os.environ,
            "IOCAGE_HOSTNAME": f"{host_hostname}",
            "IOCAGE_NAME": f"ioc-{self.uuid}",
        }

        start = su.Popen(start_cmd, stderr=su.PIPE, stdout=su.PIPE,
                         env=start_env)

        stdout_data, stderr_data = start.communicate()

        if start.returncode:
            # This is actually fatal.
            msg = "  + Start FAILED"
            iocage_lib.ioc_common.logit({
                "level": "ERROR",
                "message": msg
            },
                _callback=self.callback,
                silent=self.silent)

            iocage_lib.ioc_common.logit({
                "level": "EXCEPTION",
                "message": stderr_data.decode('utf-8')
            }, _callback=self.callback,
                silent=self.silent)
        else:
            iocage_lib.ioc_common.logit({
                "level": "INFO",
                "message": "  + Started OK"
            },
                _callback=self.callback,
                silent=self.silent)

        iocage_lib.ioc_common.logit({
            'level': 'INFO',
            'message': f'  + Using devfs_ruleset: {devfs_ruleset}'
        },
            _callback=self.callback,
            silent=self.silent)

        os_path = f"{self.path}/root/dev/log"

        if not os.path.isfile(os_path) and not os.path.islink(os_path):
            os.symlink("../var/run/log", os_path)

        vnet_err = self.start_network(vnet)

        if not vnet_err and vnet:
            iocage_lib.ioc_common.logit({
                "level": "INFO",
                "message": "  + Configuring VNET OK"
            },
                _callback=self.callback,
                silent=self.silent)

        elif vnet_err and vnet:
            iocage_lib.ioc_common.logit({
                "level": "ERROR",
                "message": "  + Configuring VNET FAILED"
            },
                _callback=self.callback,
                silent=self.silent)

            for v_err in vnet_err:
                iocage_lib.ioc_common.logit({
                    "level": "ERROR",
                    "message": f"  {v_err}"
                },
                    _callback=self.callback,
                    silent=self.silent)

            iocage_lib.ioc_stop.IOCStop(
                self.uuid, self.path, force=True, silent=True
            )

            iocage_lib.ioc_common.logit({
                "level": "EXCEPTION",
                "message": f"\nStopped {self.uuid} due to VNET failure"
            },
                _callback=self.callback)

        if self.conf["jail_zfs"] == "on":
            for jdataset in self.conf["jail_zfs_dataset"].split():
                jdataset = jdataset.strip()
                children = iocage_lib.ioc_common.checkoutput(
                    ["zfs", "list", "-H", "-r", "-o",
                     "name", "-s", "name",
                     f"{self.pool}/{jdataset}"])

                try:
                    iocage_lib.ioc_common.checkoutput(
                        ["zfs", "jail", "ioc-{}".format(self.uuid),
                         "{}/{}".format(self.pool, jdataset)],
                        stderr=su.STDOUT)
                except su.CalledProcessError as err:
                    raise RuntimeError(
                        f"{err.output.decode('utf-8').rstrip()}")

                for child in children.split():
                    child = child.strip()

                    try:
                        mountpoint = iocage_lib.ioc_common.checkoutput(
                            ["zfs", "get", "-H",
                             "-o",
                             "value", "mountpoint",
                             f"{self.pool}/{jdataset}"]).strip()

                        if mountpoint != "none":
                            iocage_lib.ioc_common.checkoutput(
                                ["setfib", self.exec_fib, "jexec",
                                 f"ioc-{self.uuid}", "zfs",
                                 "mount", child], stderr=su.STDOUT)
                    except su.CalledProcessError as err:
                        msg = err.output.decode('utf-8').rstrip()
                        iocage_lib.ioc_common.logit({
                            "level": "EXCEPTION",
                            "message": msg
                        },
                            _callback=self.callback,
                            silent=self.silent)

        self.start_generate_resolv()
        self.start_copy_localtime()
        # This needs to be a list.
        exec_start = self.conf['exec_start'].split()

        with open(
            f'{self.iocroot}/log/{self.uuid}-console.log', 'a'
        ) as f:
            success, error = '', ''
            try:
                output = iocage_lib.ioc_exec.SilentExec(
                    ['setfib', self.exec_fib, 'jexec', f'ioc-{self.uuid}']
                    + exec_start, None, unjailed=True, decode=True
                )
            except ioc_exceptions.CommandFailed as e:

                error = str(e)
                iocage_lib.ioc_stop.IOCStop(
                    self.uuid, self.path, force=True, silent=True
                )

                msg = f'  + Starting services FAILED\nERROR:\n{error}\n\n' \
                    f'Refusing to start {self.uuid}: exec_start failed'
                iocage_lib.ioc_common.logit({
                    'level': 'EXCEPTION',
                    'message': msg
                },
                    _callback=self.callback,
                    silent=self.silent
                )
            else:
                success = output.stdout
                msg = '  + Starting services OK'
                iocage_lib.ioc_common.logit({
                    'level': 'INFO',
                    'message': msg
                },
                    _callback=self.callback,
                    silent=self.silent
                )
            finally:
                f.write(f'{success}\n{error}')

        # Running exec_poststart now
        poststart_success, poststart_error = \
            iocage_lib.ioc_common.runscript(
                exec_poststart
            )

        if poststart_error:

            iocage_lib.ioc_stop.IOCStop(
                self.uuid, self.path, force=True, silent=True
            )

            iocage_lib.ioc_common.logit({
                'level': 'EXCEPTION',
                'message': '  + Executing exec_poststart FAILED\n'
                f'ERROR:\n{poststart_error}\n\nRefusing to '
                f'start {self.uuid}: exec_poststart failed'
            },
                _callback=self.callback,
                silent=self.silent
            )

        else:
            iocage_lib.ioc_common.logit({
                'level': 'INFO',
                'message': '  + Executing poststart OK'
            },
                _callback=self.callback,
                silent=self.silent
            )

        if not vnet_err and vnet and wants_dhcp:
            failed_dhcp = False

            try:
                interface = self.conf['interfaces'].split(',')[0].split(
                    ':')[0]

                if 'vnet' in interface:
                    # Jails default is epairNb
                    interface = f'{interface.replace("vnet", "epair")}b'

                # We'd like to use ifconfig -f inet:cidr here,
                # but only FreeBSD 11.0 and newer support it...
                cmd = ['jexec', f'ioc-{self.uuid}', 'ifconfig',
                       interface, 'inet']
                out = su.check_output(cmd)

                # ...so we extract the ip4 address and mask,
                # and calculate cidr manually
                addr_split = out.splitlines()[2].split()
                ip4_addr = addr_split[1].decode()
                hexmask = addr_split[3].decode()
                maskcidr = sum([bin(int(hexmask, 16)).count('1')])

                addr = f'{ip4_addr}/{maskcidr}'

                if '0.0.0.0' in addr:
                    failed_dhcp = True

            except (su.CalledProcessError, IndexError):
                failed_dhcp = True
                addr = 'ERROR, check jail logs'

            if failed_dhcp:
                iocage_lib.ioc_stop.IOCStop(
                    self.uuid, self.path, force=True, silent=True
                )

                iocage_lib.ioc_common.logit({
                    'level': 'EXCEPTION',
                    'message': '  + Acquiring DHCP address: FAILED,'
                    f' address received: {addr}\n'
                    f'\nStopped {self.uuid} due to DHCP failure'
                },
                    _callback=self.callback)

            iocage_lib.ioc_common.logit({
                'level': 'INFO',
                'message': f'  + DHCP Address: {addr}'
            },
                _callback=self.callback,
                silent=self.silent)

        self.set(
            "last_started={}".format(datetime.datetime.utcnow().strftime(
                "%F %T")))

    def check_aliases(self, ip_addrs, mode='4'):
        """
        Check if the alias already exists for given IP's, otherwise add
        default interface to the ips and return the new list
        """

        inet_mode = netifaces.AF_INET if mode == '4' else netifaces.AF_INET6
        gws = netifaces.gateways()

        try:
            def_iface = gws['default'][inet_mode][1]
        except KeyError:
            # They have no default gateway for mode 4|6
            return ip_addrs

        _ip_addrs = ip_addrs.split(',')
        interfaces_to_skip = ('vnet', 'bridge', 'epair', 'pflog')
        current_ips = []
        new_ips = []

        # We want to make sure they haven't already created
        # this alias
        for interface in netifaces.interfaces():
            if interface.startswith(interfaces_to_skip):
                continue

            with ioc_exceptions.ignore_exceptions(KeyError):
                for address in netifaces.ifaddresses(interface)[inet_mode]:
                    current_ips.append(address['addr'])

        for ip in _ip_addrs:
            if '|' not in ip:
                ip = ip if ip in current_ips else f'{def_iface}|{ip}'

            new_ips.append(ip)

        return ','.join(new_ips)

    def start_network(self, vnet):
        """
        This function is largely a check to see if VNET is true, and then to
        actually run the correct function, otherwise it passes.

        :param vnet: Boolean
        """
        errors = []

        if not vnet:
            return

        _, jid = iocage_lib.ioc_list.IOCList().list_get_jid(self.uuid)
        net_configs = (
            (self.get("ip4_addr"), self.get("defaultrouter"), False),
            (self.get("ip6_addr"), self.get("defaultrouter6"), True))
        nics = self.get("interfaces").split(",")

        vnet_default_interface = self.get('vnet_default_interface')
        if (
                vnet_default_interface != 'auto'
                and vnet_default_interface != 'none'
                and vnet_default_interface not in netifaces.interfaces()
        ):
            # Let's not go into starting a vnet at all if the default
            # interface is supplied incorrectly
            return [
                'Set property "vnet_default_interface" to "auto", "none" or a'
                'valid interface e.g "lagg0"'
            ]

        for nic in nics:
            err = self.start_network_interface_vnet(nic, net_configs, jid)

            if err:
                errors.extend(err)

        if len(errors) != 0:
            return errors

    def start_network_interface_vnet(self, nic_defs, net_configs, jid):
        """
        Start VNET on interface

        :param nic_defs: comma separated interface definitions (nic, bridge)
        :param net_configs: Tuple of IP address and router pairs
        :param jid: The jails ID
        """
        errors = []

        nic_defs = nic_defs.split(",")
        nics = list(map(lambda x: x.split(":")[0], nic_defs))

        for nic_def in nic_defs:

            nic, bridge = nic_def.split(":")

            try:
                membermtu = self.find_bridge_mtu(bridge)
                dhcp = self.get("dhcp")

                ifaces = []

                for addrs, gw, ipv6 in net_configs:
                    if (
                        dhcp == "on" or 'DHCP' in self.get('ip4_addr').upper()
                    ) and 'accept_rtadv' not in addrs:
                        # Spoofing IP address, it doesn't matter with DHCP
                        addrs = f"{nic}|''"

                    if addrs == 'none':
                        continue

                    for addr in addrs.split(','):
                        try:
                            iface, ip = addr.split("|")
                        except ValueError:
                            # They didn't supply an interface, assuming default
                            iface, ip = "vnet0", addr

                        if iface not in nics:
                            continue

                        if iface not in ifaces:
                            err = self.start_network_vnet_iface(nic, bridge,
                                                                membermtu, jid)
                            if err:
                                errors.append(err)

                            ifaces.append(iface)

                        err = self.start_network_vnet_addr(iface, ip, gw, ipv6)
                        if err:
                            errors.append(err)

            except su.CalledProcessError as err:
                errors.append(err.output.decode("utf-8").rstrip())

        if len(errors) != 0:
            return errors

    def start_network_vnet_iface(self, nic, bridge, mtu, jid):
        """
        The real meat and potatoes for starting a VNET interface.

        :param nic: The network interface to assign the IP in the jail
        :param bridge: The bridge to attach the VNET interface
        :param mtu: The mtu of the VNET interface
        :param jid: The jails ID
        :return: If an error occurs it returns the error. Otherwise, it's None
        """
        vnet_default_interface = self.get('vnet_default_interface')
        if vnet_default_interface == 'auto':
            vnet_default_interface = self.get_default_gateway()[1]

        mac_a, mac_b = self.__start_generate_vnet_mac__(nic)
        epair_a_cmd = ["ifconfig", "epair", "create"]
        epair_a = su.Popen(epair_a_cmd, stdout=su.PIPE).communicate()[0]
        epair_a = epair_a.decode().strip()
        epair_b = re.sub("a$", "b", epair_a)

        if 'vnet' in nic:
            # Inside jails they are epairN
            jail_nic = f"{nic.replace('vnet', 'epair')}b"
        else:
            jail_nic = nic

        try:
            # Host
            iocage_lib.ioc_common.checkoutput(
                [
                    "ifconfig", epair_a, "name",
                    f"{nic}.{jid}", "mtu", mtu
                ],
                stderr=su.STDOUT
            )
            iocage_lib.ioc_common.checkoutput(
                ["ifconfig", f"{nic}.{jid}", "link", mac_a],
                stderr=su.STDOUT
            )
            iocage_lib.ioc_common.checkoutput(
                ["ifconfig", f"{nic}.{jid}", "description",
                 f"associated with jail: {self.uuid} as nic: {jail_nic}"],
                stderr=su.STDOUT
            )

            if 'accept_rtadv' in self.get('ip6_addr'):
                # Set linklocal for IP6 + rtsold
                iocage_lib.ioc_common.checkoutput(
                    ['ifconfig', f'{nic}.{jid}', 'inet6', 'auto_linklocal',
                     'accept_rtadv', 'autoconf'],
                    stderr=su.STDOUT)

            # Jail
            iocage_lib.ioc_common.checkoutput(
                [
                    "ifconfig", epair_b, "vnet",
                    f"ioc-{self.uuid}"
                ],
                stderr=su.STDOUT
            )
            iocage_lib.ioc_common.checkoutput(
                [
                    'jexec', f'ioc-{self.uuid}', 'ifconfig', epair_b,
                    'mtu', mtu
                ],
                stderr=su.STDOUT
            )

            if epair_b != jail_nic:
                # This occurs on default vnet0 ip4_addr's
                iocage_lib.ioc_common.checkoutput(
                    [
                        "setfib", self.exec_fib, "jexec", f"ioc-{self.uuid}",
                        "ifconfig", epair_b, "name", jail_nic
                    ],
                    stderr=su.STDOUT
                )

            iocage_lib.ioc_common.checkoutput(
                [
                    "setfib", self.exec_fib, "jexec", f"ioc-{self.uuid}",
                    "ifconfig", jail_nic, "link", mac_b
                ],
                stderr=su.STDOUT
            )

            try:
                # Host interface as supplied by user also needs to be on the
                # bridge
                if vnet_default_interface != 'none':
                    iocage_lib.ioc_common.checkoutput(
                        ["ifconfig", bridge, "addm", vnet_default_interface],
                        stderr=su.STDOUT
                    )
            except su.CalledProcessError:
                # Already exists
                pass

            iocage_lib.ioc_common.checkoutput(
                ["ifconfig", bridge, "addm", f"{nic}.{jid}", "up"],
                stderr=su.STDOUT
            )
            iocage_lib.ioc_common.checkoutput(
                ["ifconfig", f"{nic}.{jid}", "up"],
                stderr=su.STDOUT
            )
        except su.CalledProcessError as err:
            return f"{err.output.decode('utf-8')}".rstrip()

    def start_network_vnet_addr(self, iface, ip, defaultgw, ipv6=False):
        """
        Add an IP address to a vnet interface inside the jail.

        :param iface: The interface to use
        :param ip:  The IP address to assign
        :param defaultgw: The gateway IP to assign to the nic
        :return: If an error occurs it returns the error. Otherwise, it's None
        """
        dhcp = self.get('dhcp')
        wants_dhcp = True if dhcp == 'on' or 'DHCP' in self.get(
            'ip4_addr').upper() else False

        if 'vnet' in iface:
            # Inside jails they are epairNb
            iface = f'{iface.replace("vnet", "epair")}b'

        if ipv6:
            ifconfig = [iface, 'inet6', ip, 'up']
            # set route to none if this is not the first interface
            if iface.rsplit('epair')[1][0] == '0':
                route = ['add', '-6', 'default', defaultgw]
            else:
                route = 'none'
        else:
            ifconfig = [iface, ip, 'up']
            # set route to none if this is not the first interface
            if iface.rsplit('epair')[1][0] == '0':
                route = ['add', 'default', defaultgw]
            else:
                route = 'none'

        try:
            if not wants_dhcp and ip != 'accept_rtadv':
                # Jail side
                iocage_lib.ioc_common.checkoutput(
                    ['setfib', self.exec_fib, 'jexec', f'ioc-{self.uuid}',
                     'ifconfig'] + ifconfig, stderr=su.STDOUT)
                # route has value of none if this is not the first interface
                if route != 'none':
                    iocage_lib.ioc_common.checkoutput(
                        ['setfib', self.exec_fib, 'jexec', f'ioc-{self.uuid}',
                         'route'] + route, stderr=su.STDOUT)
        except su.CalledProcessError as err:
            return f'{err.output.decode("utf-8")}'.rstrip()
        else:
            return

    def start_copy_localtime(self):
        host_time = self.get("host_time")
        file = f"{self.path}/root/etc/localtime"

        if host_time != "yes":
            return

        if os.path.isfile(file):
            os.remove(file)

        try:
            shutil.copy("/etc/localtime", file, follow_symlinks=False)
        except FileNotFoundError:
            return

    def start_generate_resolv(self):
        resolver = self.get("resolver")
        #                                     compat

        if resolver != "/etc/resolv.conf" and resolver != "none" and \
                resolver != "/dev/null":
            with iocage_lib.ioc_common.open_atomic(
                    f"{self.path}/root/etc/resolv.conf", "w") as resolv_conf:

                for line in resolver.split(";"):
                    resolv_conf.write(line + "\n")
        elif resolver == "none":
            shutil.copy("/etc/resolv.conf",
                        f"{self.path}/root/etc/resolv.conf")
        elif resolver == "/dev/null":
            # They don't want the resolv.conf to be touched.

            return
        else:
            shutil.copy(resolver, f"{self.path}/root/etc/resolv.conf")

    def __generate_mac_bytes(self, nic):
        m = hashlib.md5()
        m.update(self.uuid.encode("utf-8"))
        m.update(nic.encode("utf-8"))
        prefix = self.get("mac_prefix")

        return f"{prefix}{m.hexdigest()[0:12-len(prefix)]}"

    def __generate_mac_address_pair(self, nic):
        mac_a = self.__generate_mac_bytes(nic)
        mac_b = hex(int(mac_a, 16) + 1)[2:].zfill(12)

        return mac_a, mac_b

    def __start_generate_vnet_mac__(self, nic):
        """
        Generates a random MAC address and checks for uniquness.
        If the jail already has a mac address generated, it will return that
        instead.
        """
        mac = self.get("{}_mac".format(nic))

        if mac == "none":
            mac_a, mac_b = self.__generate_mac_address_pair(nic)
            self.set(f"{nic}_mac={mac_a} {mac_b}")
        else:
            try:
                mac_a, mac_b = mac.replace(',', ' ').split()
            except Exception:
                iocage_lib.ioc_common.logit({
                    "level": "EXCEPTION",
                    "message": f'Please correct mac addresses format for {nic}'
                })

        return mac_a, mac_b

    def __check_dhcp__(self):
        # legacy behavior to enable it on every NIC
        if self.conf['dhcp'] == 'on':
            nic_list = self.get('interfaces').split(',')
            nics = list(map(lambda x: x.split(':')[0], nic_list))
        else:
            nics = []
            for ip4 in self.conf['ip4_addr'].split(','):
                nic, addr = ip4.rsplit('/', 1)[0].split('|')

                if addr.upper() == 'DHCP':
                    nics.append(nic)

        for nic in nics:
            if 'vnet' in nic:
                # Inside jails they are epairNb
                nic = f"{nic.replace('vnet', 'epair')}b"

            su.run(
                [
                    'sysrc', '-f', f'{self.path}/root/etc/rc.conf',
                    f'ifconfig_{nic}=SYNCDHCP'
                ],
                stdout=su.PIPE
            )

    def __check_rtsold__(self):
        if 'accept_rtadv' not in self.conf['ip6_addr']:
            iocage_lib.ioc_common.logit(
                {
                    'level': 'EXCEPTION',
                    'message':
                        'Must set at least one ip6_addr to accept_rtadv!'
                },
                _callback=self.callback,
                silent=self.silent
            )

        su.run(
            [
                'sysrc', '-f', f'{self.path}/root/etc/rc.conf',
                f'rtsold_enable=YES'
            ],
            stdout=su.PIPE
        )

    def get_default_gateway(self):
        # e.g response - ('192.168.122.1', 'lagg0')
        try:
            return netifaces.gateways()["default"][netifaces.AF_INET]
        except KeyError:
            iocage_lib.ioc_common.logit(
                {
                    'level': 'EXCEPTION',
                    'message': 'No default gateway interface found'
                },
                _callback=self.callback,
                silent=self.silent
            )

    def get_bridge_members(self, bridge):
        return [
            x.split()[1] for x in
            iocage_lib.ioc_common.checkoutput(
                ["ifconfig", bridge]
            ).splitlines()
            if x.strip().startswith("member")
        ]

    def find_bridge_mtu(self, bridge):
        if self.unit_test:
            dhcp = 'off'
            wants_dhcp = False
        else:
            dhcp = self.get("dhcp")
            wants_dhcp = True if dhcp == 'on' or 'DHCP' in self.get(
                'ip4_addr').upper() else False

        try:
            if wants_dhcp:
                # Let's get the default vnet interface
                default_if = self.get('vnet_default_interface')
                if default_if == 'auto':
                    default_if = self.get_default_gateway()[1]

                if default_if != 'none':
                    bridge_cmd = [
                        "ifconfig", bridge, "create", "addm", default_if
                    ]
                    su.check_call(bridge_cmd, stdout=su.PIPE, stderr=su.PIPE)

            else:
                bridge_cmd = ["ifconfig", bridge, "create", "addm"]
                su.check_call(bridge_cmd, stdout=su.PIPE, stderr=su.PIPE)
        except su.CalledProcessError:
            # The bridge already exists, this is just best effort.
            pass

        memberif = self.get_bridge_members(bridge)
        if not memberif:
            return '1500'

        membermtu = iocage_lib.ioc_common.checkoutput(
            ["ifconfig", memberif[0]]
        ).split()

        return membermtu[5]
