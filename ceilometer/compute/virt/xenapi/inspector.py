# Copyright 2014 Intel
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License. You may obtain
# a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and limitations
# under the License.
"""Implementation of Inspector abstraction for XenAPI."""

from oslo_config import cfg
from oslo_utils import units
import six.moves.urllib.parse as urlparse
try:
    import XenAPI as api
except ImportError:
    api = None

try:
    import cPickle as pickle
except ImportError:
    import pickle

from ceilometer.compute.pollsters import util
from ceilometer.compute.virt import inspector as virt_inspector
from ceilometer.i18n import _

opt_group = cfg.OptGroup(name='xenapi',
                         title='Options for XenAPI')

OPTS = [
    cfg.StrOpt('connection_url',
               help='URL for connection to XenServer/Xen Cloud Platform.'),
    cfg.StrOpt('connection_username',
               default='root',
               help='Username for connection to XenServer/Xen Cloud '
                    'Platform.'),
    cfg.StrOpt('connection_password',
               help='Password for connection to XenServer/Xen Cloud Platform.',
               secret=True),
]


class XenapiException(virt_inspector.InspectorException):
    pass


def swap_xapi_host(url, host_addr):
    """Replace the XenServer address present in 'url' with 'host_addr'."""
    temp_url = urlparse.urlparse(url)
    # The connection URL is served by XAPI and doesn't support having a
    # path for the connection url after the port. And username/password
    # will be pass separately. So the URL like "http://abc:abc@abc:433/abc"
    # should not appear for XAPI case.
    temp_netloc = temp_url.netloc.replace(temp_url.hostname, '%s' % host_addr)
    replaced = temp_url._replace(netloc=temp_netloc)
    return urlparse.urlunparse(replaced)


def get_api_session(conf):
    if not api:
        raise ImportError(_('XenAPI not installed'))

    url = conf.xenapi.connection_url
    username = conf.xenapi.connection_username
    password = conf.xenapi.connection_password
    if not url or password is None:
        raise XenapiException(_('Must specify connection_url, and '
                                'connection_password to use'))

    try:
        session = (api.xapi_local() if url == 'unix://local'
                   else api.Session(url))
        session.login_with_password(username, password)
    except api.Failure as e:
        if e.details[0] == 'HOST_IS_SLAVE':
            master = e.details[1]
            url = swap_xapi_host(url, master)
            try:
                session = api.Session(url)
                session.login_with_password(username, password)
            except api.Failure as es:
                raise XenapiException(_('Could not connect slave host: %s') %
                                      es.details[0])
        else:
            msg = _("Could not connect to XenAPI: %s") % e.details[0]
            raise XenapiException(msg)
    return session


class XenapiInspector(virt_inspector.Inspector):

    def __init__(self, conf):
        super(XenapiInspector, self).__init__(conf)
        self.session = get_api_session(self.conf)
        self.host_ref = self._get_host_ref()
        self.host_uuid = self._get_host_uuid()

    def _get_host_ref(self):
        """Return the xenapi host on which nova-compute runs on."""
        return self.session.xenapi.session.get_this_host(self.session.handle)

    def _get_host_uuid(self):
        return self.session.xenapi.host.get_uuid(self.host_ref)

    def _call_xenapi(self, method, *args):
        return self.session.xenapi_request(method, args)

    def _call_plugin(self, plugin, fn, args):
        args['host_uuid'] = self.host_uuid
        return self.session.xenapi.host.call_plugin(
            self.host_ref, plugin, fn, args)

    def _call_plugin_serialized(self, plugin, fn, *args, **kwargs):
        params = {'params': pickle.dumps(dict(args=args, kwargs=kwargs))}
        rv = self._call_plugin(plugin, fn, params)
        return pickle.loads(rv)

    def _lookup_by_name(self, instance_name):
        vm_refs = self._call_xenapi("VM.get_by_name_label", instance_name)
        n = len(vm_refs)
        if n == 0:
            raise virt_inspector.InstanceNotFoundException(
                _('VM %s not found in XenServer') % instance_name)
        elif n > 1:
            raise XenapiException(
                _('Multiple VM %s found in XenServer') % instance_name)
        else:
            return vm_refs[0]

    def inspect_instance(self, instance, duration=None):
        instance_name = util.instance_name(instance)
        vm_ref = self._lookup_by_name(instance_name)
        cpu_util = self._get_cpu_usage(vm_ref, instance_name)
        memory_usage = self._get_memory_usage(vm_ref)
        return virt_inspector.InstanceStats(cpu_util=cpu_util,
                                            memory_usage=memory_usage)

    def _get_cpu_usage(self, vm_ref, instance_name):
        vcpus_number = int(self._call_xenapi("VM.get_VCPUs_max", vm_ref))
        if vcpus_number <= 0:
            msg = _("Could not get VM %s CPU number") % instance_name
            raise XenapiException(msg)
        cpu_util = 0.0
        for index in range(vcpus_number):
            cpu_util += float(self._call_xenapi("VM.query_data_source",
                                                vm_ref,
                                                "cpu%d" % index))
        return cpu_util / int(vcpus_number) * 100

    def _get_memory_usage(self, vm_ref):
        total_mem = float(self._call_xenapi("VM.query_data_source",
                                            vm_ref,
                                            "memory"))
        try:
            free_mem = float(self._call_xenapi("VM.query_data_source",
                                               vm_ref,
                                               "memory_internal_free"))
        except api.Failure:
            # If PV tools is not installed in the guest instance, it's
            # impossible to get free memory. So give it a default value
            # as 0.
            free_mem = 0
        # memory provided from XenServer is in Bytes;
        # memory_internal_free provided from XenServer is in KB,
        # converting it to MB.
        return (total_mem - free_mem * units.Ki) / units.Mi

    def inspect_vnics(self, instance):
        instance_name = util.instance_name(instance)
        vm_ref = self._lookup_by_name(instance_name)
        dom_id = self._call_xenapi("VM.get_domid", vm_ref)
        vif_refs = self._call_xenapi("VM.get_VIFs", vm_ref)
        bw_all = self._call_plugin_serialized('bandwidth',
                                              'fetch_all_bandwidth')
        for vif_ref in vif_refs or []:
            vif_rec = self._call_xenapi("VIF.get_record", vif_ref)

            interface = virt_inspector.Interface(
                name=vif_rec['uuid'],
                mac=vif_rec['MAC'],
                fref=None,
                parameters=None)
            bw_vif = bw_all[dom_id][vif_rec['device']]

            # TODO(jianghuaw): Currently the plugin can only support
            # rx_bytes and tx_bytes, so temporarily set others as -1.
            stats = virt_inspector.InterfaceStats(
                rx_bytes=bw_vif['bw_in'], rx_packets=-1, rx_drop=-1,
                rx_errors=-1, tx_bytes=bw_vif['bw_out'], tx_packets=-1,
                tx_drop=-1, tx_errors=-1)
            yield (interface, stats)

    def inspect_vnic_rates(self, instance, duration=None):
        instance_name = util.instance_name(instance)
        vm_ref = self._lookup_by_name(instance_name)
        vif_refs = self._call_xenapi("VM.get_VIFs", vm_ref)
        if vif_refs:
            for vif_ref in vif_refs:
                vif_rec = self._call_xenapi("VIF.get_record", vif_ref)

                rx_rate = float(self._call_xenapi(
                    "VM.query_data_source", vm_ref,
                    "vif_%s_rx" % vif_rec['device']))
                tx_rate = float(self._call_xenapi(
                    "VM.query_data_source", vm_ref,
                    "vif_%s_tx" % vif_rec['device']))

                interface = virt_inspector.Interface(
                    name=vif_rec['uuid'],
                    mac=vif_rec['MAC'],
                    fref=None,
                    parameters=None)
                stats = virt_inspector.InterfaceRateStats(rx_rate, tx_rate)
                yield (interface, stats)

    def inspect_disk_rates(self, instance, duration=None):
        instance_name = util.instance_name(instance)
        vm_ref = self._lookup_by_name(instance_name)
        vbd_refs = self._call_xenapi("VM.get_VBDs", vm_ref)
        if vbd_refs:
            for vbd_ref in vbd_refs:
                vbd_rec = self._call_xenapi("VBD.get_record", vbd_ref)

                disk = virt_inspector.Disk(device=vbd_rec['device'])
                read_rate = float(self._call_xenapi(
                    "VM.query_data_source", vm_ref,
                    "vbd_%s_read" % vbd_rec['device']))
                write_rate = float(self._call_xenapi(
                    "VM.query_data_source", vm_ref,
                    "vbd_%s_write" % vbd_rec['device']))
                disk_rate_info = virt_inspector.DiskRateStats(
                    read_bytes_rate=read_rate,
                    read_requests_rate=0,
                    write_bytes_rate=write_rate,
                    write_requests_rate=0)
                yield(disk, disk_rate_info)
