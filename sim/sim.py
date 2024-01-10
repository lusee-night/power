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
        self.current    = np.zeros(size, dtype=float) # Total current drawn by the electronics
        self.battery    = np.zeros(size, dtype=float) # Battery charge
        self.ssd        = np.zeros(size, dtype=float) # Storage
# ---
class Simulator:
    def __init__(self, orbitals_f=None, modes_f=None, devices_f=None, comtable_f=None, initial_time=None, until=None):
    
        self.verbose      = False

        self.record       = {}

        # Filenames
        self.orbitals_f   = orbitals_f
        self.modes_f      = modes_f
        self.devices_f    = devices_f
        self.comtable_f   = comtable_f

        # Stubs for the Orbitals
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
        self.read_combtable()


        self.initial_time   = initial_time
        self.until          = until

        if initial_time is not None:
            self.env = simpy.Environment(initial_time=initial_time)
        else:
            self.env = simpy.Environment()

        self.populate() # -FIXME- Needs work

        self.env.process(self.run()) # Set the callback to this class, for simpy

    # ---
    def populate(self): # Add hardware and the monitor to keep track of the sim
        initial_charge,     battery_capacity = 100., 1200.  # battery
        initial_data,       ssd_capacity     = 2., 256.     # SSD

        self.battery    = Battery(self.env, initial_charge, battery_capacity)
        self.ssd        = SSD(self.env, initial_data, ssd_capacity)

        print(f'''Created a Battery with initial charge: {self.battery.level}, capacity: {self.battery.capacity}''')

        self.monitor    = Monitor(self.sun.N) # to define the discrete time axis
        self.controller = Controller(self.env, self.sun)

        Controller.verbose = True

        self.controller.add_all_panels()
        self.controller.calculate_power()

    # ---
    def read_orbitals(self):
        f = h5py.File(self.orbitals_f, "r")

        ds_meta = f["/meta/configuration"] # Expect YAML payload
        conf    = yaml.safe_load(ds_meta[0,])
        self.deltaT  = conf['period']['deltaT']

        ds_data = f["/data/orbitals"]
        da = np.array(ds_data[:]) # data array
        print(f'''Shape of the data payload: {da.shape}''')
        self.sun = Sun(da[:,0], da[:,1] , da[:,2])
        self.lpf = Sat(da[:,0], da[:,3] , da[:,4])
        self.bge = Sat(da[:,0], da[:,5] , da[:,6])

    # ---
    def read_modes(self):
        f = open(self.modes_f, 'r')
        self.modes = yaml.safe_load(f)

    # ---
    def read_devices(self):
        f = open(self.devices_f, 'r')
        profiles = yaml.safe_load(f)  # ingest the configuration data
        for device_name in profiles.keys():
            device = Device(device_name, profiles[device_name])
            self.devices[device.name]=device
    
    # ---
    def read_combtable(self):
        f = open(self.comtable_f, 'r')
        self.comtable = yaml.safe_load(f)

        for k in self.comtable.keys():
            self.schedule[self.comtable[k]['start']] = k
 
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
    def current(self):
        cur = 0.0
        for dk in self.devices.keys():
            cur+=self.devices[dk].current()
        return cur

    def set_state(self, mode):
        for dk in self.devices.keys():
            self.devices[dk].state = mode[dk]

    def device_report(self):
        for dk in self.devices.keys():
            print(self.devices[dk].info())
        print('*** Total current:', self.current())


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
        with open(filename, 'w') as file:
            yaml.dump(self.record, file)

    ############################## Simulation code #############################
    
    def simulate(self):
        if self.until is not None:
            self.env.run(until=self.until) # 17760
        else:
            self.env.run()
    
    def run(self): # SimPy machinery: print(f'''Clock: {self.sun.mjd[myT]}, power: {Panel.profile[myT]}''')
    
        mode = None
        charge_current = 0.001 # arbitrary value for BMS current

        cnt = 0

        while True:
            myT     = int(self.env.now)
            clock   = self.sun.mjd[myT]

            sched   = self.find_schedule(clock)
            md = sched['mode']

            if md!=mode:
                mode = md
                self.set_state(self.modes[mode])

                if self.verbose:
                    print(f'''Clock:{clock}, mode: {mode}''')
                    print('Device states:', self.modes[mode])
                    self.device_report()

                cnt+=1
                self.record[cnt] = {'start': float(clock), 'mode': mode}
                

            # Electrical section:
            self.monitor.current[myT] = self.current()
            try:
                if (self.modes[mode]['bms'] == 'ON'): # See if the battery is charging:
                    charge   = self.controller.power[myT]*self.deltaT*charge_current # charge current is just a scaling factor here
                    self.battery.put(charge)
            except:
                pass

            try:
                if (self.modes[mode]['spectrometer'] == 'ON'): # See if there is data produced
                    data = self.deltaT*0.0001 # FIXME just a scaling factor for now
                    self.ssd.put(data)
            except:
                pass


            # Draw charge from battery
            draw_charge = self.current()*0.1
            self.battery.get(draw_charge)

            self.monitor.battery[myT]   = self.battery.level
            self.monitor.ssd[myT]       = self.ssd.level

            yield self.env.timeout(1)

