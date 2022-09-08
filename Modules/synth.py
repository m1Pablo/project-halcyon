# Project Halcyon - Synth
# synth.py
# author: @m1pablo

# folder
folder="/home/pi/Documents/CodeLibrary/Synth"

# libraries
import os
import sys
import time
import random
from typing import Optional
import pandas as pd
from rich import pretty
pretty.install()
from rich.progress import Progress, SpinnerColumn, TimeElapsedColumn, BarColumn
from rich.panel import Panel
from rich.console import Console
from rich import box
from rich.table import Table
import rtmidi
from rtmidi.midiutil import open_midiinput

# modules
sys.path.insert(0, f"{folder}/Modules")
import menu
import midi

# classes
class Screen:
	def __init__(self, action, message_1, message_2, midi_in_name, midi_out_name):
		self.action=action
		self.message_1=message_1
		self.message_2=message_2
		self.midi_in_name=midi_in_name
		self.midi_out_name=midi_out_name

class Midi:
	def __init__(self, midi_in, midi_out, midi_in_port, midi_in_port_name):
		self.midi_in=midi_in
		self.midi_out=midi_out
		self.midi_in_port=midi_in_port
		self.midi_in_port_name=midi_in_port_name

#functions
def cls(screen):
	global quit
	header_to_print=""
	message_1_to_print=""
	message_2_to_print=""
	if not quit:
		os.system("cls" if os.name == "nt" else "clear")
		header_to_print+=":musical_keyboard: Synth"
		console.print(Panel(f'{header_to_print}'))
	if screen.message_2 is not None:
		message_2_to_print+=screen.message_2
		console.print(Panel(f'{message_2_to_print}'))
	if screen.message_1 is not None:
		message_1_to_print+=screen.message_1
		console.print(Panel(f'{message_1_to_print}'))
	return None

def midi_in_is_available(midi_in_name):
	global screen
	(midi_ports,midi_names)=midi.get_midi_list()
	midi_in_available=True
	if midi_in_name not in midi_names:
		screen.message_2=f'{random_monkey()} Midi-In [deep_sky_blue1]{midi_in_name}[/deep_sky_blue1] disconnected!'				
		my_midi.midi_in.close_port()
		my_midi.midi_in=None
		my_midi.midi_in_port=None
		my_midi.midi_in_port_name=None
		cls(screen)
		screen.midi_in_name=None
		midi_in_available=False
	return midi_in_available
		

def cls_remote():
	global screen
	return cls(screen)

def random_monkey():
	monkeys=[":see_no_evil:",":speak_no_evil:",":hear_no_evil:"]
	monkey=monkeys[random.randint(0,2)]
	return monkey

# globals
quit=False
back=False
action=None
message_1=None
message_2=None
midi_in=None
midi_in_name=None
midi_in_port=None
midi_in_port_name=None
midi_out=None
midi_out_name=None
my_midi=Midi(midi_in,midi_out,midi_in_port,midi_in_port_name)
screen=Screen(action, message_1, message_2,midi_in_name, midi_out_name)
console = Console()
actions_list=["Show status","Select Midi-In","Open Midi-Out","Poll Midi-In","Delete instrument"]

# main
def main(*args):
	global my_midi, screen, quit
	cls(screen)

	for arg in args:
		my_midi=arg
	while not quit:
				
		if screen.action is None:
			[(idx,screen.action),screen.message_1]=menu.selector(actions_list,"action")
			screen.message_2=None
			cls(screen)

		if screen.action in ["quit","q"]:
			quit=True
			cls(screen)
			break

		if screen.action in ["back","b"]:
			screen.action=None
			screen.message_1=None	
			screen.message_2=None
			cls(screen)

		if screen.midi_in_name is not None:
			midi_in_is_available(screen.midi_in_name)

		if screen.action in ["Select Midi-In"]:
			(midi_ports,midi_names)=midi.get_midi_list()
			[(idx,midi_name),screen.message_1]=menu.selector(midi_names,"midi")
			if midi_name in ["quit","q"]:
				quit=True
				cls(screen)
				break
			elif midi_name in ["back","b"]:
				screen.action=None
				screen.message_1=None
				screen.message_2=None
				cls(screen)
			else:
				try:
					if midi_in_is_available(midi_name):
						midi_port_name=midi_ports[idx]
						my_midi.midi_in, my_midi.midi_in_port_name = open_midiinput(midi_port_name)
						my_midi.midi_port=idx
						screen.midi_in_name = midi_name
						screen.action=None
						screen.message_1=f":musical_keyboard: Midi-In [deep_sky_blue1]{screen.midi_in_name}[/deep_sky_blue1] selected!"
						screen.message_2=None
				except Exception as e:
					print(e)
					pass
				cls(screen)
		elif screen.action in ["Poll Midi-In"]:
			if my_midi.midi_in is not None:
				screen.message_1=midi.poll_midi_in(my_midi.midi_in,screen.midi_in_name)	
			else:
				screen.message_1=f'{random_monkey()} No Midi-In selected.'
			screen.action=None
			cls(screen)
		elif screen.action in ["Show status"]:
			screen.message_1=f':musical_keyboard: Midi-In: [deep_sky_blue1]{screen.midi_in_name}[/deep_sky_blue1]\n'
			screen.message_1+=f':headphones: Midi-Out: [deep_sky_blue1]{screen.midi_out_name}[deep_sky_blue1]'
			screen.action=None
			cls(screen)
		elif screen.action in ["Open Midi-Out"]:
			
		elif screen.action in [None]:
			pass
		else:
			screen.message_1=f"{random_monkey()} Action [deep_sky_blue1]{screen.action}[/deep_sky_blue1] not yet available. Come back later."
			screen.action=None
			cls(screen)
	return (screen,my_midi)

# run

if __name__ == "__main__":
	main()

# To run code, save first, then input in terminal:
# python3 Documents/CodeLibrary/Halcyon/Modules/synth.py