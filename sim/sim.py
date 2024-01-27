# foundation packages
import  simpy
import  yaml
import  h5py

# local packages
from    hardware import *
from    utils.timeconv import *
from    nav import *  # Astro/observation wrapper classes

#################################################################################
class Monitor():
    def __init__(self, size=0):
        # Time series --
        self.power      = np.zeros(size, dtype=float) # Total power drawn by the electronics
        self.battery    = np.zeros(size, dtype=float) # Battery charge
        self.data_rate  = np.zeros(size, dtype=float) # data rate in/out of the system
        self.ssd        = np.zeros(size, dtype=float) # Storage
# ---
class Simulator:
    def __init__(self, orbitals_f=None, modes_f=None, devices_f=None, comtable_f=None, initial_time=None, until=None):
    
        self.verbose    = False
        self.record     = {} # stub for the record of state transitions

        # Filenames (input data)
        self.orbitals_f = orbitals_f
        self.modes_f    = modes_f
        self.devices_f  = devices_f
        self.comtable_f = comtable_f

        # Stubs for the Orbitals data
        self.sun        = None
        self.lpf        = None
        self.bge        = None
        
        # Stubs for other stuff
        self.modes      = None
        self.comtable   = None
        self.schedule   = {}
        self.devices    = {}

        # Metadata to be read with orbitals; can add more if needed
        self.deltaT     = None

        # Read all inputs
        self.read_orbitals()
        self.read_devices()
        self.read_modes()

        if comtable_f is not None: self.read_comtable()

        self.initial_time   = initial_time
        self.until          = until

        if initial_time is not None:
            self.env = simpy.Environment(initial_time=initial_time)
        else:
            self.env = simpy.Environment()

        self.populate()

        self.create_command_table = False
        self.comm_tx = False

        self.env.process(self.run()) # Set the callback to this class, for simpy

    # ---
    def populate(self): # Add hardware and the monitor to keep track of the sim

        Wh2Ch = 1/28*3600 # /28V * 3600s/h

        self.battery    = Battery(self.env, self.battery_initial_Wh*Wh2Ch, self.battery_capacity_Wh*Wh2Ch)
        if self.verbose: print(f'''Created a Battery with initial charge: {self.battery.level}, capacity: {self.battery.capacity}''')

        self.ssd        = SSD(self.env, self.ssd_initial, self.ssd_capacity)
        if self.verbose:  print(f'''Created a SSD with initial fill: {self.ssd.level}, capacity: {self.ssd.capacity}''')
        
        self.monitor    = Monitor(self.sun.N) # to define the discrete time axis
        self.controller = Controller(self.env, self.sun)

        Controller.verbose = self.verbose

        self.controller.add_panels_from_config(self.panel_config)
        self.controller.calculate_power()

    # ---
    def read_orbitals(self):
        """ Read previously calculated data on the coordinates of the Sun and the Satellites.
            The file name is expected to be provides in the attribute orbitals_f.
        """        

        f = h5py.File(self.orbitals_f, "r")

        ds_meta = f["/meta/configuration"] # Expect YAML payload
        conf    = yaml.safe_load(ds_meta[0,])
        self.deltaT  = conf['period']['deltaT']

        ds_data = f["/data/orbitals"]
        da = np.array(ds_data[:]) # data array
        if self.verbose: print(f'''Shape of the data payload: {da.shape}''')
        self.sun = Sun(da[:,0], da[:,1] , da[:,2])
        self.lpf = Sat(da[:,0], da[:,3] , da[:,4])
        self.bge = Sat(da[:,0], da[:,5] , da[:,6])

    # ---
    def read_modes(self):
        f = open(self.modes_f, 'r')
        self.modes = yaml.safe_load(f)

    # ---
    def read_devices(self):
        """ Initialize devices using data read from the 'devices file' (YAML)
        """
        f                       = open(self.devices_f, 'r')
        profiles                = yaml.safe_load(f)  # "hold all" dictionary
    
        power_consumer_devices  = profiles['power_consumers'].keys()
        ssd_consumer_devices    = profiles['ssd_consumers'].keys()
        device_names            = power_consumer_devices | ssd_consumer_devices

        if 'bms' not in device_names:
            print('BMS not found in the device list')
            raise NotImplementedError

        if 'comms' not in device_names:
            print('Comms not found in the device list')
            raise NotImplementedError

        for device_name in device_names:
            power_profile               = profiles['power_consumers'].get(device_name)
            data_profile                = profiles['ssd_consumers'].get(device_name)
            self.devices[device_name]   = Device(device_name, power_profile, data_profile)
        

        # Component data, read from the "devices" file
        self.battery_capacity_Wh    = float(profiles['battery']['capacity'])
        self.battery_initial_Wh     = float(profiles['battery']['initial'])
        self.charge_efficiency      = float(profiles['battery']['charge_efficiency'])
        self.discharge_efficiency   = float(profiles['battery']['discharge_efficiency'])

        self.ssd_capacity           = float(profiles['ssd']['capacity'])
        self.ssd_initial            = float(profiles['ssd']['initial'])

        self.panel_config           = profiles['solar_panels']
    
    # ---
    def read_comtable(self):
        f = open(self.comtable_f, 'r')
        self.comtable = yaml.safe_load(f)

        for k in self.comtable.keys(): self.schedule[self.comtable[k]['start']] = k
        self.times = list(self.schedule.keys())

    # ---
    def find_schedule(self, clock):
        l = len(self.times) - 1 
        tmax = self.times[l]
        if clock>=tmax:
            return self.comtable[self.schedule[tmax]]

        ndx = 0
        for t in self.times:
            if clock>=t:
                ndx+=1
            else:
                theTime = self.times[ndx-1]
                return self.comtable[self.schedule[theTime]]
   
        return None

    # ---
    def init_generate_schedule(self, myT):
        day = self.sun.alt[myT] > 0
        self.last_day_state = day
        if day:
            self.last_sunrise_mjd = self.sun.mjd[myT]
        else:
            self.last_sunset_mjd = self.sun.mjd[myT]
        self.last_comm = self.sun.mjd[myT]

    # --
    def generate_schedule(self, myT):
        ## all of this is purely placeholder now
        ## don't take it too seriously

        sched = {}
        day = self.sun.alt[myT] > 0
        mjd_now = self.sun.mjd[myT]
        if not day:
            if self.last_day_state:
                # day to night transition
                self.last_sunset_mjd = self.sun.mjd[myT]
            # let's switch between science and powersave every 12 hours
            tick = (mjd_now - self.last_sunset_mjd) / 0.5
            if int(tick)%3==0:
                sched['mode'] = 'science'
            else:
                sched['mode'] = 'powersave'
        else:
            if not self.last_day_state:
                # night to day transition
                self.last_sunrise_mjd = self.sun.mjd[myT]
            comm_opportunity = self.lpf.alt[myT] > 0.1
            need_comm = ( (mjd_now-self.last_comm)>4  # we haven't talked for a while
                            or (self.battery.level <0.2*self.battery.capacity)  # we are low on battery so might as well use this opportunity
                            or ((self.battery.level<0.8*self.battery.capacity) and (mjd_now-self.last_sunrise_mjd)>12)) # we have two days to fully charge
            calib_opportunity = self.bge.alt[myT] > -0.1
            if calib_opportunity:
                sched['mode'] = 'science'
            else:
                if comm_opportunity and need_comm:
                    sched['mode'] = 'comms'
                    self.last_comm = self.sun.mjd[myT]
                else:
                    sched['mode'] = 'science'
        self.last_day_state = day
        return sched

    # ---
    def power(self):
        pwr = 0.0
        for dk in self.devices.keys():
            if dk=='comms' and self.comm_tx:
                pwr += self.devices[dk].power_tx()
            else:
                pwr += self.devices[dk].power()
        return pwr
    
    def data_rate(self):
        dr = 0.0
        for dk in self.devices.keys():
            if dk=='comms' and self.comm_tx:
                dr+=self.devices[dk].data_rate_tx()
            else:
                dr+=self.devices[dk].data_rate()
        return dr

    def set_state(self, mode):
        for dk in self.devices.keys():
            self.devices[dk].state = mode[dk]

    def device_report(self):
        if self.verbose:        
            for dk in self.devices.keys(): print(self.devices[dk].info())
            print('*** Total power:', self.power(),'W')


    def info(self):
        print(f'''Orbitals file: {self.orbitals_f}''')

        print('------------------')
        print(f'''Modes file: {self.modes_f}''')
        print(pretty(self.modes))

        print('------------------')
        print(f'''Devices file: {self.devices_f}''')

        self.device_report()


        print('------------------')
        print(f'''Comtable file: {self.comtable_f}''')
        print(pretty(self.comtable))

        print('------------------')
        print(f'''Day condition at start and end of the simulation: {self.sun.day[self.initial_time]}, {self.sun.day[self.until]}''')

    def save_record(self, filename='simulator_log.yml'):
        """ Capture the generated state transition record.
            It's in the same format as the main command table.
        """
        with open(filename, 'w') as file:
            yaml.dump(self.record, file)

    ############################## Simulation code #############################
    # ---
    def simulate(self, create_command_table = False):
        self.create_command_table = create_command_table
        if create_command_table:
            myT     = int(self.env.now)
            self.init_generate_schedule(myT)

        if self.until is not None:
            self.env.run(until=self.until) # 17760
        else:
            self.env.run()
    # ---
    def run(self): # SimPy machinery: print(f'''Clock: {self.sun.mjd[myT]}, power: {Panel.profile[myT]}''')
        mode = None
        cnt = 0

        while True:
            myT     = int(self.env.now)
            clock   = self.sun.mjd[myT]

            if self.create_command_table:
                sched = self.generate_schedule(myT)
            else:
                sched  = self.find_schedule(clock)
            
            md = sched['mode']

            if md!=mode:
                mode = md
                self.set_state(self.modes[mode])

                if self.verbose:
                    print(f'''Clock:{clock}, mode: {mode}''')
                    print('Device states:', self.modes[mode])
                    self.device_report()

                cnt+=1
                
                battery_fill = float(self.battery.level/self.battery.capacity)
                ssd_fill = self.ssd.level/self.ssd.capacity
                self.record[cnt] = {'start': float(clock), 
                                    'mode': mode,
                                    'battery_expected_fill': battery_fill,
                                    'ssd_expected_fill': ssd_fill}
                
            if (self.lpf.alt[myT]>0.1) and (self.modes[mode]['comms'] == 'ON'):
                self.comm_tx = True
            else:
                self.comm_tx = False


            # Electrical section:
            self.monitor.power[myT] = self.power()
            if (self.modes[mode]['bms'] == 'ON'): # See if the battery is charging:
                charge   = self.controller.power[myT]*self.deltaT/28  # FIXME - hardcoded 28V volts here

                if self.verbose: print ('charge',charge, self.controller.power[myT])

                self.battery.put(charge*self.charge_efficiency)
        
            # Draw charge from battery
            draw_charge = self.power()*self.deltaT/28  # FIXME - hardcoded 28V volts here
            self.battery.get(draw_charge/self.discharge_efficiency)
            self.monitor.battery[myT]   = self.battery.level/self.battery.capacity

            # Data section
            ## first are we communicating:
            data_rate = self.data_rate()    
            self.monitor.data_rate[myT] = data_rate
            self.ssd.change(data_rate*self.deltaT)
            self.monitor.ssd[myT]       = self.ssd.level/self.ssd.capacity

            yield self.env.timeout(1)

