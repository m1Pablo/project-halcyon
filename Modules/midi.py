# Project Halcyon - Synth
# midi.py
# authors: pablo.mendoza

# libraries
import sys
import pandas as pd
from rich import print, pretty
from rich.panel import Panel
from rich.console import Console
import time
pretty.install()
import rtmidi
from rtmidi.midiutil import open_midioutput
from rtmidi.midiconstants import NOTE_OFF, NOTE_ON
quit=False

# functions
def get_midi_list():
    midi_ports=[]
    midi_names=[]
    ports_list=rtmidi.MidiIn().get_ports()
    for i,midi in enumerate(ports_list):
        if midi==("Midi Through:Midi Through Port-0 14:0"):
            midi_names.append("Midi Through Port")
        elif midi==("Akai LPK25 Wireless:Akai LPK25 Wireless MIDI 1 20:0"):
            midi_names.append("Akai LPK25 Wireless Midi")
        else:
            midi_names.append(midi)
        midi_ports.append(i)
    return (midi_ports,midi_names)

def poll_midi_in(midi_in,port_name,*argv):
    try:
        timer = timer_start = time.time()
        print("\n :repeat: Entering main loop. Press Control-C to exit.")
        time_elapsed=0
        while True:
            msg = midi_in.get_message()
            if msg:
                message, deltatime = msg
                timer += deltatime
                time_elapsed=round(timer-timer_start,2)
                if message[0]==NOTE_OFF:
                    key_emoji="fast_up_button"
                    Key_press="Note off"
                    key_press_midi=NOTE_OFF
                elif message[0]==NOTE_ON:
                    key_emoji="fast_down_button"
                    key_press="Note on"
                    key_press_midi=NOTE_ON
                if message[2]<=127/3:
                    speaker_emoji="speaker_low_volume"
                elif message[2]<=127/2:
                    speaker_emoji="speaker_medium_volume"
                else:
                    speaker_emoji="speaker_high_volume"
                print(f"\n :musical_keyboard: {port_name} | :hourglass_flowing_sand: {time_elapsed} | :musical_note: {message[1]} | :{key_emoji}: {key_press} | :{speaker_emoji}: {message[2]}")
                try:
                    note_on = [key_press_midi, message[1], message[2]]
                    midiout.send_message(note_on)
                except:
                    pass
            time.sleep(0.01)
    except KeyboardInterrupt:
        print('')
    finally:
        return (f":stop_button: Poll stopped. Duration: {time_elapsed}s")


