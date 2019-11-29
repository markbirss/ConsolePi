#!/etc/ConsolePi/venv/bin/python3

import json
import threading
import time
from collections import OrderedDict as od
from os import path
from sys import stdin

import requests
import RPi.GPIO as GPIO
from consolepi.dlirest import DLI
from halo import Halo

try:
    import better_exceptions
    better_exceptions.MAX_LENGTH = None
except ImportError:
    print('no better')

TIMING = False
CYCLE_TIME = 3

class Outlets:

    def __init__(self, power_file='/etc/ConsolePi/power.json', log=None):
        # pylint: disable=maybe-no-member
        self.power_file = power_file
        self.spin = Halo(spinner='dots')
        GPIO.setmode(GPIO.BCM)
        GPIO.setwarnings(False)
        self._dli = {}
        self.outlet_data = {}



    def do_tasmota_cmd(self, address, command=None):
        '''
        Perform Operation on Tasmota outlet:
        params:
            address: IP or resolvable hostname
            command: 
                True | 'ON': power the outlet on
                False | 'OFF': power the outlet off
                'Toggle': Toggle the outlet
                'cycle': Cycle Power on outlets that are powered On
        TODO: Right now this method does not verify if port is currently in an ON state
            before allowing 'cycle', resulting in it powering on the port consolepi-menu
            verifies status before allowing the command, but *may* be that other outlet
            are handled by this library.. check & make consistent
        TODO: remove int returns and always return string on error for consistency
        '''
        # sub to make the api call to the tasmota device
        def tasmota_req(*args, **kwargs):
            querystring = kwargs['querystring']
            try:
                response = requests.request("GET", url, headers=headers, params=querystring, timeout=3)
                if response.status_code == 200:
                    if json.loads(response.text)['POWER'] == 'ON':
                        _response = True
                    elif json.loads(response.text)['POWER'] == 'OFF':
                        _response = False
                    else:
                        _response = 'invalid state returned {}'.format(response.text)
                else:
                    _response = '[{}] error returned {}'.format(response.status_code, response.text)
            except requests.exceptions.Timeout:
                _response = 'Reqest Timed Out'
            except requests.exceptions.RequestException as e:
                _response = 'Exception Occured: {}'.format(e)
            return _response
        # -------- END SUB --------

        url = 'http://' + address + '/cm'
        headers = {
            'Cache-Control': "no-cache",
            'Connection': "keep-alive",
            'cache-control': "no-cache"
            }
        
        cycle = False
        if command is not None:
            if isinstance(command, bool):
                command = 'ON' if command else 'OFF'
            
            command = command.upper()
            if command in ['ON', 'OFF', 'TOGGLE']:
                querystring = {"cmnd":"Power {}".format(command)}
            elif command == 'CYCLE': # Power off if cycle is command, powered back on below
                if tasmota_req(querystring={"cmnd":"Power"}):
                    querystring = {"cmnd":"Power OFF"}
                    cycle = True
                else:
                    return 'Cycle is only valid for ports that are Currently ON'
            else:
                raise KeyError
        else: # if no command specified return the status of the port
            querystring = {"cmnd":"Power"}

        # -- // Send Request to TASMOTA \\ --
        r = tasmota_req(querystring=querystring)
        if cycle:
            if not r:
                time.sleep(CYCLE_TIME)
                r = tasmota_req(querystring={"cmnd":"Power ON"})
            else:
                return 'Unexpected response, port returned on state expected off'
        return r

    # @Halo(text='Loading', spinner='dots')
    def load_dli(self, address, username, password):
        '''
        Returns instace of DLI class if class has not been instantiated for the provided 
        dli web power switch.  returns an existing instance if it's already been instantiated.
        returns True if class already loaded or False if class was instantiated during this call
        '''
        if address not in self._dli or not self._dli[address]:
            # -- // Load the DLI \\--
            if stdin.isatty():
                self.spin.start('[DLI] Getting Outlets {}'.format(address))
                # print('[DLI] Getting Outlets {}'.format(address))
            self._dli[address] = DLI(address, username, password)

            # --// Return Pass or fail based on reachability \\--
            if not self._dli[address].reachable:
                if stdin.isatty():
                    self.spin.fail()
                return None, None
            else:
                if stdin.isatty():
                    self.spin.succeed()
                return self._dli[address], False

        # --// DLI Already Loaded \\--
        else:
            return self._dli[address], True

    def get_outlets(self, upd_linked=False, failures={}):
        '''
        Get Outlet details
        params:
            upd_linked: True will update just the linked ports, False is for dli and will update
                all ports for the dli.
            failures: when refreshing outlets pass in previous failures so they can be re-tried
        '''
        if not self.outlet_data:
            if path.isfile(self.power_file):
                with open (self.power_file, 'r') as _power_file:
                    outlet_data = _power_file.read()
                outlet_data = json.loads(outlet_data, object_pairs_hook=od)
            else:
                outlet_data = None
                return
        else:
            outlet_data = self.outlet_data['linked'] if 'linked' in self.outlet_data else None
            if failures: # re-attempt connection to failed power controllers on refresh
                outlet_data = {**outlet_data, **failures}

        
        failures = {}
        if outlet_data is not None: # Nothing in power.json or file doesn't exist
            dli_power = {} if 'dli_power' not in self.outlet_data else self.outlet_data['dli_power']
            for k in outlet_data:
                outlet = outlet_data[k]
                if outlet['type'].upper() == 'GPIO':
                    noff = True if 'noff' not in outlet else outlet['noff'] # default normally off to True if not provided
                    GPIO.setup(outlet['address'], GPIO.OUT)  # pylint: disable=maybe-no-member
                    outlet['is_on'] = bool(GPIO.input(outlet['address'])) if noff else not bool(GPIO.input(outlet['address'])) # pylint: disable=maybe-no-member
                elif outlet['type'] == 'tasmota':
                    response = self.do_tasmota_cmd(outlet['address'])
                    outlet['is_on'] = response
                    if response not in [0, 1, True, False]:
                        failures[k] = outlet_data[k]
                        failures[k]['error'] = '[PWR-TASMOTA {}:{}] Returned Error - Removed'.format(
                            k, failures[k]['address']) 
                elif outlet['type'].lower() == 'dli':
                    if TIMING:
                        dbg_line = '------------------------ // NOW PROCESSING {} \\\\ ------------------------'.format(k)
                        print('\n{}'.format('=' * len(dbg_line)))
                        print('{}\n{}\n{}'.format(dbg_line, outlet_data[k], '-' * len(dbg_line)))
                        print('{}'.format('=' * len(dbg_line)))
                    # - Check the power.json data for some required information
                    all_good = True # initial value
                    for _ in ['address', 'username', 'password']:
                        if _ not in outlet or outlet[_] is None:
                            all_good = False
                            failures[k] = outlet_data[k]
                            failures[k]['error'] = '[PWR-DLI {}] {} missing from {} configuration - skipping'.format(k, _, failures[k]['address']) 
                            # Log here, delete item from dict? TODO
                            break
                    if all_good:
                        (this_dli, _update) = self.load_dli(outlet['address'], outlet['username'], outlet['password'])
                        if this_dli is None or this_dli.dli is None:
                            failures[k] = outlet_data[k]
                            failures[k]['error'] = '[PWR-DLI {}] {} Unreachable - Removed'.format(k, failures[k]['address'])
                        else:
                            if TIMING:
                                xstart = time.time()
                                print('this_dli.outlets: {} {}'.format(this_dli.outlets, 'update' if _update else 'init'))
                                print(json.dumps(dli_power, indent=4, sort_keys=True))
                            if _update:
                                if not upd_linked:
                                    dli_power[outlet['address']] = this_dli.get_dli_outlets()
                                    if not dli_power[outlet['address']]:
                                        all_good = False
                                    if all_good and 'linked_ports' in outlet and outlet['linked_ports'] is not None:
                                        _p = outlet['linked_ports']
                                        if isinstance(_p, int):
                                            outlet['is_on'] = {_p: this_dli.outlets[_p]}
                                        else:
                                            outlet['is_on'] = {}
                                            for _ in _p:
                                                outlet['is_on'][_] = dli_power[outlet['address']][_]
                                else:
                                    if 'linked_ports' in outlet and outlet['linked_ports']:
                                        _p = outlet['linked_ports']
                                        outlet['is_on'] = this_dli[_p]
                                        # TODO not actually using the error returned this turned into a hot mess
                                        if isinstance(outlet['is_on'], dict) and not outlet['is_on']:
                                            all_good = False
                                        # update dli_power for the refreshed ports
                                        else:
                                            for _ in outlet['is_on']:
                                                dli_power[outlet['address']][_] = outlet['is_on'][_]

                                # handle error connecting to dli during refresh - when connect worked on menu launch
                                if not all_good:
                                    failures[k] = outlet_data[k]
                                    failures[k]['error'] = '[PWR-DLI {}] {} Unreachable - Removed'.format(k, failures[k]['address'])
                            else:
                                dli_power[outlet['address']] = this_dli.outlets
                                if 'linked_ports' in outlet and outlet['linked_ports'] is not None:
                                    _p = outlet['linked_ports']
                                    if isinstance(_p, int):
                                        outlet['is_on'] = {_p: this_dli.outlets[_p]}
                                    else:
                                        outlet['is_on'] = {}
                                        for _ in _p:
                                            outlet['is_on'][_] = dli_power[outlet['address']][_]
                            if TIMING:
                                print('[TIMING] this_dli.outlets: {}'.format(time.time() - xstart)) # TIMING

        # Move failed outlets from the keys that populate the menu to the 'failures' key
        # failures are displayed in the footer section of the menu, then re-tried on refresh
        for _dev in failures:
            # print(failures[_dev]['error'])
            if _dev in outlet_data:
                del outlet_data[_dev]
            if failures[_dev]['address'] in dli_power:
                del dli_power[failures[_dev]['address']]
        self.outlet_data = {
            'linked': outlet_data,
            'failures': failures,
            'dli_power': dli_power
            }
        return self.outlet_data

    def pwr_toggle(self, pwr_type, address, desired_state=None, port=None, noff=True, noconfirm=False):   # TODO refactor to pwr_toggle 
        '''Toggle Power On the specified port

        args:
            pwr_type: valid types = 'dli', 'tasmota', 'GPIO' (not case sensitive)
            address: for dli and tasmota: str - ip or fqdn
        kwargs:
            desired_state: bool The State True|False (True = ON) you want the outlet to be in
                if not provided the method will query the current state of the port and set desired_state to the inverse
            port: Only required for dli: can be type str | int | list.  
                valid: 
                    int: representing the dli outlet #
                    list: list of outlets(int) to perform operation on
                    str: 'all' ~ to perform operation on all outlets
            noff: Bool, default: True.  = normally off, only applies to GPIO based outlets.
                If an outlet is normally off (True) = the relay/outlet is off if no power is applied via GPIO
                Setting noff to False flips the ON/OFF evaluation so the menu will show the port is ON when no power is applied.

        returns: 
            Bool representing resulting port state (True = ON)
        '''
        # --// REMOVE ONCE VERIFIED \\--
        if isinstance(desired_state, str): # menu should be passing in True/False no on off now. can remove once that's verified
            desired_state = False if desired_state.lower() == 'off' else True
            print('\ndev_note: pwr_toggle passed str not bool for desired_state check calling function {}'.format(desired_state))
            time.sleep(5)
        
        # -- // Toggle dli web power switch port \\ --
        if pwr_type.lower() == 'dli':
            if port is not None:
                response = self._dli[address].toggle(port, toState=desired_state)
            # else:
            #     raise Exception('pwr_toggle: port must be provided for outlet type dli')
            
        # -- // Toggle GPIO port \\ --
        elif pwr_type.upper() == 'GPIO':
            gpio = address
            # get current state and determine inverse if toggle called with no desired_state specified
            if desired_state is None:
                cur_state = bool(GPIO.input(gpio)) if noff else not bool(GPIO.input(gpio)) # pylint: disable=maybe-no-member
                desired_state = not cur_state
            if desired_state:
                GPIO.output(gpio, int(noff)) # pylint: disable=maybe-no-member
            else: 
                GPIO.output(gpio, int(not noff))  # pylint: disable=maybe-no-member
            response = bool(GPIO.input(gpio)) if noff else not bool(GPIO.input(gpio)) # pylint: disable=maybe-no-member

        # -- // Toggle TASMOTA port \\ --
        elif pwr_type.lower() == 'tasmota':
            if desired_state is None:
                desired_state = not self.do_tasmota_cmd(address)
            response = self.do_tasmota_cmd(address, desired_state)

        else:
            raise Exception('pwr_toggle: Invalid type ({}) or no name provided'.format(pwr_type))

        return response

    def pwr_cycle(self, pwr_type, address, port=None, noff=True):
        '''
        returns Bool True = Power Cycle success, False Not performed Outlet OFF
            TODO Check error handling if unreachable
        '''
        pwr_type = pwr_type.lower()
        # --// CYCLE DLI PORT \\--
        if pwr_type == 'dli':
            if port is not None:
                response = self._dli[address].cycle(port)
            else:
                raise Exception('pwr_cycle: port must be provided for outlet type dli')

        # --// CYCLE GPIO PORT \\--
        elif pwr_type == 'gpio':
            # normally off states are normal 0:off 1:on - if not normally off it's reversed 0:on 1:off
            # pylint: disable=maybe-no-member
            gpio = address
            cur_state = GPIO.input(gpio) if noff else not GPIO.input(gpio)
            if cur_state:
                GPIO.output(gpio, int(not noff))
                time.sleep(CYCLE_TIME)
                GPIO.output(gpio, int(noff))
                response = bool(GPIO.input(gpio))
                response = response if noff else not response
            else:
                response = False  # Cycle is not valid on ports that are alredy off

        # --// CYCLE TASMOTA PORT \\--
        elif pwr_type == 'tasmota':
            response = self.do_tasmota_cmd(address)
            if response:  # Only Cycle if outlet is currently ON
                response = self.do_tasmota_cmd(address, 'cycle')

        return response

    def pwr_rename(self, type, address, name=None, port=None):
        if name is None:
            try:
                name = input('New name for {} port: {} >> '.format(address, port))
            except KeyboardInterrupt:
                print('Rename Aborted!')
                return 'Rename Aborted'
        if type.lower() == 'dli':
            if port is not None:
                response = self._dli[address].rename(port, name)
                if response:
                    self.outlet_data['dli_power'][address][port]['name'] = name
            else:
                response = 'ERROR port must be provided for outlet type dli'
        elif (type.upper() == 'GPIO' or type.lower() == 'tasmota'):
            print('rename of GPIO and tasmota ports not yet implemented')
            print('They can be renamed manually by updating power.json')
            response = 'rename of GPIO and tasmota ports not yet implemented'
            # TODO get group name based on address, read json file into dict, change the name write it back
            #      and update dict 
        else:
            raise Exception('pwr_rename: Invalid type ({}) or no name provided'.format(type))

        return response

    def pwr_all(self, outlets=None, action='toggle', desired_state=None):
        '''
        Returns List of responses representing state of outlet after exec
            Valid response is Bool where True = ON
            Errors are returned in str format
        '''
        if action == 'toggle' and desired_state is None:
            return 'Error: desired final state must be provided' # should never hit this

        if outlets is None:
            outlets = self.get_outlets
        responses = []
        for grp in outlets:
            outlet = outlets[grp]
            noff = True if 'noff' not in outlet else outlet['noff']
            if action == 'toggle':
                # skip any defined dlis that don't have any linked_outlets defined
                if not outlet['type'] == 'dli' or  ('linked_ports' in outlet and outlet['linked_ports']):
                    responses.append(self.pwr_toggle(outlet['type'], outlet['address'], desired_state=desired_state,
                    port=outlet['linked_ports'] if outlet['type'] == 'dli' and 'linked_ports' in outlet else None,
                    noff=noff, noconfirm=True))
            elif action == 'cycle':
                if outlet['type'] != 'dli':
                    threading.Thread(
                            target=self.pwr_cycle, 
                            args=[outlet['type'], outlet['address']], 
                            kwargs={'noff': noff}, 
                            name='cycle_{}'.format(outlet['address'])
                        ).start()
                elif 'linked_ports' in outlet:
                    if isinstance(outlet['linked_ports'], int):
                        linked_ports = [outlet['linked_ports']]
                    else:
                        linked_ports = outlet['linked_ports']
                    for p in linked_ports:
                        # Start a thread for each port run in parallel
                        # menu status for (linked) power menu is updated on load
                        threading.Thread(
                                target=self.pwr_cycle,
                                args=[outlet['type'], outlet['address']],
                                kwargs={'port': p, 'noff': noff},
                                name='cycle_{}'.format(p)
                            ).start()

        # Wait for all threads to complete
        while True:
            threads = 0
            for t in threading.enumerate():
                if 'cycle' in t.name:
                    threads += 1
            if threads == 0:
                break

        return responses


    # Does not appear to be used can prob remove
    def get_state(self, type, address, port=None):
        if type.upper() == 'GPIO':
            GPIO.setup(address)  # pylint: disable=maybe-no-member
            response = GPIO.input(address)  # pylint: disable=maybe-no-member
        elif type.lower() == 'tasmota':
            response = self.do_tasmota_cmd(address)
        elif type.lower() == 'dli':
            if port is not None:
                response = self._dli[address].state(port)
            else:
                response = 'ERROR no port provided for dli port'

        return response

    def confirm(self, prompt, action='Toggle'):
        '''
        returns Bool: False = User aborted operation
        '''
        while True:
            choice = input(prompt)
            ch = choice.lower()
            if ch in ['y', 'yes', 'n', 'no']:
                if ch in ['n', 'no']:
                    return '{} {{red}}OFF{{norm}} Aborted by user'.format(action)
                else:
                    return True
            else:
                print('Invalid Response: {}'.format(choice))

if __name__ == '__main__':
    pwr = Outlets('/etc/ConsolePi/power.json')
    outlets = pwr.get_outlets()
    print(json.dumps(outlets, indent=4, sort_keys=True))
    # upd = pwr.get_outlets(upd_linked=True)
    # print(json.dumps(upd, indent=4, sort_keys=True))
