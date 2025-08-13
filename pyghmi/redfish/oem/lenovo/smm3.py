# Copyright 2025 Lenovo Corporation
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import copy
import os
import pyghmi.redfish.oem.generic as generic
import pyghmi.constants as pygconst
import pyghmi.util.webclient as webclient
import pyghmi.exceptions as exc
import time
import socket

healthlookup = {
    'ok': pygconst.Health.Ok,
    'critical': pygconst.Health.Critical
}

def _baytonumber(bay):
    try:
        return int(bay)
    except ValueError:
        if len(bay) == 2:
            # Treat a hexadecimal system as a leading decimal digit and letter compile
            # 1a == slot 1, 1b == slot 2, 2a == slot 1, etc..
            try:
                tmp = int(bay, 16)
                return (2 * (tmp >> 4) - 1) + ((tmp & 15) % 10)
            except ValueError:
                return None
    return None


def _baytolabel(bay):
    try:
        baynum =  int(bay)
        if baynum < 1:
            raise exc.UnsupportedFunctionality(
                    'Reseat not supported for whole chassis')
        # need to convert to 1a, 1b, etc...
        vertidx = ((baynum - 1) // 2 + 1) << 4
        horizidx = (baynum - 1) % 2 + 10
        bayid = vertidx | horizidx
        return '{:02x}'.format(bayid)
    except ValueError:
        return bay
    return None

class OEMHandler(generic.OEMHandler):
    def get_health(self, fishclient, verbose=True):
        rsp = self._do_web_request('/redfish/v1/Chassis/chassis1')
        health = rsp.get('Status', {}).get('Health', 'Unknown').lower()
        health = healthlookup.get(health, pygconst.Health.Critical)
        return {'health': health}

    def set_identify(self, on=True, blink=False):
        if on:
            state = 'On'
        elif blink:
            state = 'Blinking'
        else:
            state = 'Off'
        self._do_web_request('/redfish/v1/Chassis/chassis1', {
            'Oem': {'Lenovo': {'LED': {'IdentifyLED': {
                'State': state
                }}}
            }}, method='PATCH')

    def get_system_configuration(self, hideadvanced=True, fishclient=None):
        return {}

    def retrieve_firmware_upload_url(self):
        # SMMv3 needs to do the non-multipart upload
        usd = self._do_web_request('/redfish/v1/UpdateService', cache=False)
        if usd.get('HttpPushUriTargetsBusy', False):
                raise exc.TemporaryError('Cannot run multtiple updates to '
                                            'same target concurrently')
        try:
            upurl = usd['HttpPushUri']
        except KeyError:
            raise exc.UnsupportedFunctionality('Redfish firmware update only supported for implementations with push update support')
        if 'HttpPushUriTargetsBusy' in usd:
            self._do_web_request(
                '/redfish/v1/UpdateService',
                {'HttpPushUriTargetsBusy': True}, method='PATCH')
        return usd,upurl,False

    def continue_update(self, uploadthread, progress):
        # SMMv3 does not provide a response, must hardcode the continuation
        # /redfish/v1/UpdateService/FirmwareInventory/fwuimage
        rsp = self._do_web_request('/redfish/v1/UpdateService/FirmwareInventory/fwuimage')
        for ri in rsp.get('RelatedItem', []):
            targ = ri.get('@odata.id', None)
        parms = {'Oem': {'Lenovo': {'SecureRollBack': False}}}
        rsp = self._do_web_request('/redfish/v1/UpdateService', parms, method='PATCH')
        targspec = {'target': targ}
        rsp = self._do_web_request('/redfish/v1/UpdateService/Actions/UpdateService.StartUpdate', targspec)
        monitorurl = rsp.get('@odata.id', None)
        return self.monitor_update_progress(monitorurl, progress)
        


    def get_diagnostic_data(self, savefile, progress=None, autosuffix=False):
        tsk = self._do_web_request(
            '/redfish/v1/Managers/bmc/LogServices/Dump/Actions/LogService.CollectDiagnosticData',
            {"DiagnosticDataType": "Manager"})
        taskrunning = True
        taskurl = tsk.get('@odata.id', None)
        pct = 0 if taskurl else 100
        durl = None
        while pct < 100 and taskrunning:
            status = self._do_web_request(taskurl)
            durl = status.get('AdditionalDataURI', '')
            pct = status.get('PercentComplete', 0)
            taskrunning = status.get('TaskState', 'Complete') == 'Running'
            if progress:
                progress({'phase': 'initializing', 'progress': float(pct)})
            if taskrunning:
                time.sleep(3)
        if not durl:
            for hdr in status.get('Payload', {}).get('HttpHeaders', []):
                if hdr.startswith('Location: '):

                    enturl = hdr.replace('Location: ', '')
                    entryinfo = self._do_web_request(enturl)
                    durl = entryinfo.get('AdditionalDataURI', None)
                    break
        if not durl:
            raise Exception("Failed getting service data url")
        fname = os.path.basename(durl)
        if autosuffix and not savefile.endswith('.tar.xz'):
            savefile += time.strftime('-SMM3_%Y%m%d_%H%M%S.tar.xz')
        fd = webclient.FileDownloader(self.webclient, durl, savefile)
        fd.start()
        while fd.isAlive():
            fd.join(1)
            if progress and self.webclient.get_download_progress():
                progress({'phase': 'download',
                          'progress': 100 * self.webclient.get_download_progress()})
        if fd.exc:
            raise fd.exc
        if progress:
            progress({'phase': 'complete'})
        return savefile

    def _extract_fwinfo(self, inf):
        fwi, url = inf
        currinf = {}
        buildid = fwi.get('Oem', {}).get('Lenovo', {}).get('ExtendedVersion', None)
        if buildid:
            currinf['build'] = buildid
        return currinf


    def _get_node_info(self):
        nodeinfo = self._varsysinfo
        if not nodeinfo:
            overview = self._do_web_request('/redfish/v1/')
            chassismembs = overview.get('Chassis', {}).get('@odata.id', None)
            if not chassismembs:
                return nodeinfo
            chassislist = self._do_web_request(chassismembs)
            chassismembs = chassislist.get('Members', [])
            if len(chassismembs) == 1:
                chassisurl = chassismembs[0]['@odata.id']
                nodeinfo = self._do_web_request(chassisurl)
        newnodeinfo = copy.deepcopy(nodeinfo)
        newnodeinfo['SKU'] = nodeinfo['Model']
        newnodeinfo['Model'] = 'N1380 Enclosure'
        return newnodeinfo

    def reseat_bay(self, bay):
        bayid = _baytolabel(bay)
        url = '/redfish/v1/Chassis/chassis1/Oem/Lenovo/Nodes/{}/Actions/Node.Reseat'.format(bayid)
        rsp = self._do_web_request(url, method='POST')

    def get_event_log(self, clear=False, fishclient=None):
        return super().get_event_log(clear, fishclient, extraurls=[{'@odata.id':'/redfish/v1/Chassis/chassis1/LogServices/EventLog'}])

    def get_description(self, fishclient):
        return {'height': 13, 'slot': 0, 'slots': [8, 2]}
