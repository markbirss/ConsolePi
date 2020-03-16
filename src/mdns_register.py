#!/etc/ConsolePi/venv/bin/python3

from zeroconf import ServiceInfo, Zeroconf
import time
import json
import socket
import pyudev
import threading
import struct
import sys
try:
    import better_exceptions  # NoQA pylint: disable=import-error
except ImportError:
    pass

sys.path.insert(0, '/etc/ConsolePi/src/pypkg')
from consolepi import log, config  # NoQA
from consolepi.consolepi import ConsolePi  # NoQA
from consolepi.gdrive import GoogleDrive  # NoQA


UPDATE_DELAY = 30


class MDNS_Register:

    def __init__(self):
        self.zeroconf = Zeroconf()
        self.context = pyudev.Context()
        self.cpi = ConsolePi()

    def build_info(self, squash=None, local_adapters=None):
        local = self.cpi.local
        loc = local.build_local_dict(refresh=True).get(local.hostname, {})
        loc['hostname'] = local.hostname
        for a in loc['adapters']:
            if 'udev' in loc['adapters'][a]:
                del loc['adapters'][a]['udev']

        ip_w_gw = loc['interfaces'].get('_ip_w_gw', '127.0.0.1')

        # if data set is too large for mdns browser on other side will retrieve via API
        if squash is not None:
            if squash == 'interfaces':
                loc['interfaces'] = {_if: loc['interfaces'][_if]
                                     for _if in loc['interfaces'] if '.' not in _if}
                loc['interfaces'] = json.dumps(loc['interfaces'])
                loc['adapters'] = json.dumps(loc['adapters'])
            else:
                del loc['adapters']
                loc['interfaces'] = json.dumps(loc['interfaces'])
        else:
            loc['interfaces'] = json.dumps(loc['interfaces'])
            loc['adapters'] = json.dumps(loc['adapters'])
        # local_json = json.dumps(loc)

        log.debug('[MDNS REG] Current content of local_data \n{}'.format(json.dumps(loc, indent=4, sort_keys=True)))
        # print(struct.calcsize(json.dumps(local_data).encode('utf-8')))

        info = ServiceInfo(
            "_consolepi._tcp.local.",
            local.hostname + "._consolepi._tcp.local.",
            addresses=[socket.inet_aton(ip_w_gw)],
            port=5000,
            properties=loc,
            server=f'{local.hostname}.local.'
        )

        return info

    def update_mdns(self, device=None, action=None, *args, **kwargs):
        zeroconf = self.zeroconf
        info = self.try_build_info()

        def sub_restart_zc():
            log.info('[MDNS REG] mdns_refresh thread Start... Delaying {} Seconds'.format(UPDATE_DELAY))
            time.sleep(UPDATE_DELAY)  # Wait x seconds and then update, to accomodate multiple add removes
            zeroconf.update_service(info)
            zeroconf.unregister_service(info)
            time.sleep(5)
            zeroconf.register_service(info)
            log.info('[MDNS REG] mdns_refresh thread Completed')

        if device is not None:
            abort_mdns = False
            for thread in threading.enumerate():
                if 'mdns_refresh' in thread.name:
                    log.debug('[MDNS REG] Another mdns_refresh thread already queued, this thread will abort')
                    abort_mdns = True
                    break

            if not abort_mdns:
                threading.Thread(target=sub_restart_zc, name='mdns_refresh', args=()).start()
                log.debug('[MDNS REG] mdns_refresh Thread Started.  Current Threads:\n    {}'.format(threading.enumerate()))

            log.info('[MDNS REG] detected change: {} {}'.format(device.action, device.sys_name))
            if config.cloud:     # pylint: disable=maybe-no-member
                abort = False
                for thread in threading.enumerate():
                    if 'cloud_update' in thread.name:
                        log.debug('[MDNS REG] Another cloud Update thread already queued, this thread will abort')
                        abort = True
                        break

                if not abort:
                    threading.Thread(target=self.trigger_cloud_update, name='cloud_update', args=()).start()
                    log.debug('[MDNS REG] Cloud Update Thread Started.  Current Threads:\n    {}'.format(threading.enumerate()))

    def try_build_info(self):
        # TODO Figure out how to calculate the struct size
        # Try sending with all data
        local = self.cpi.local
        try:
            info = self.build_info()
        except struct.error as e:
            log.debug('[MDNS REG] data is too big for mdns, removing adapter data \n    {} {}'.format(e.__class__.__name__, e))
            log.debug('[MDNS REG] offending payload \n    {}'.format(json.dumps(local.data, indent=4, sort_keys=True)))
            # Too Big - Try sending without adapter data
            try:
                info = self.build_info(squash='adapters')
            except struct.error as e:
                log.warning('[MDNS REG] data is still too big for mdns, reducing interface payload \n'
                            '    {} {}'.format(e.__class__.__name__, e))
                log.debug('[MDNS REG] offending interface data \n    {}'.format(
                          json.dumps(local.interfaces, indent=4, sort_keys=True)))

        return info

    def trigger_cloud_update(self):
        local = self.cpi.local
        remotes = self.cpi.remotes
        log.info('[MDNS REG] Cloud Update triggered delaying {} seconds'.format(UPDATE_DELAY))
        time.sleep(UPDATE_DELAY)  # Wait 30 seconds and then update, to accomodate multiple add removes
        # TODO change advertised user when option added to config to specify
        data = local.build_local_dict(refresh=True)
        if 'udev' in local[local.hostname]['adapters']:
            del local[local.hostname]['adapters']['udev']

        log.debug(f'[MDNS REG] Final Data set collected for {local.hostname}: \n{json.dumps(data)}')

        if config.cloud_svc == 'gdrive':  # pylint: disable=maybe-no-member
            cloud = GoogleDrive(log)

        remote_consoles = cloud.update_files(data)

        # Send remotes learned from cloud file to local cache
        if len(remote_consoles) > 0 and 'Gdrive-Error' not in remote_consoles:
            remotes.update_local_cloud_file(remote_consoles)
            log.info('[MDNS REG] Cloud Update Completed, Found {} Remote ConsolePis'.format(len(remote_consoles)))
        else:
            log.warn('[MDNS REG] Cloud Update Completed, No remotes found, or Error Occured')

    def run(self):
        zeroconf = self.zeroconf
        info = self.try_build_info()

        zeroconf.register_service(info)
        # monitor udev for add/remove of usb-serial adapters
        monitor = pyudev.Monitor.from_netlink(self.context)
        monitor.filter_by('usb')
        observer = pyudev.MonitorObserver(monitor, name='udev_monitor', callback=self.update_mdns)
        observer.start()
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            pass
        finally:
            print("Unregistering...")
            zeroconf.unregister_service(self.build_info())
            zeroconf.close()
            observer.send_stop()


if __name__ == '__main__':
    mdnsreg = MDNS_Register()
    mdnsreg.run()
