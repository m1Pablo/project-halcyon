# Project Halcyon
# Halcyon
# author: @m1pablo

# folder
folder="/home/pi/Documents/CodeLibrary/Halcyon"

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
	def __init__(self, action, message_1, message_2,midi_type,midi_icon):
		self.action=action
		self.message_1=message_1
		self.message_2=message_2
		self.midi_type=midi_type
		self.midi_icon=midi_icon

class Instrument:
	def __init__(self, midi_type,midi_icon):
		self.midi_type=midi_type
		self.midi_icon=midi_icon

#functions
def cls(screen):
	global quit
	header_to_print=""
	message_1_to_print=""
	message_2_to_print=""
	if not quit:
		os.system("cls" if os.name == "nt" else "clear")
		header_to_print+="[bold cyan]Project Halcyon[/bold cyan]\n"
		header_to_print+="[italic bold dark_orange](pre-alpha)[/italic bold dark_orange]"
		console.print(Panel(f'{header_to_print}'))
	if screen.message_2 is not None:
		message_2_to_print+=screen.message_2
		console.print(Panel(f'{message_2_to_print}'))
	if screen.message_1 is not None:
		message_1_to_print+=screen.message_1
		console.print(Panel(f'{message_1_to_print}'))
	return None


def cls_remote():
	global screen
	return cls(screen)

def random_icon(icons,exception_list):
	exception_icons=[]
	for exception in exception_list:
		exception_icons.append(exception.midi_icon)
	icons=list(filter(lambda icon: icon not in exception_icons,icons))
	random_icons=[]
	for i in range(0,4):
		icon=icons[random.randint(0,len(icons)-1)]
		while icon in random_icons:
			icon=icons[random.randint(0,len(icons)-1)]
		random_icons.append(icon)
	return random_icons

def random_monkey():
	monkeys=["see_no_evil","speak_no_evil","hear_no_evil"]
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
midi_type=None
midi_icon=None
screen=Screen(action, message_1, message_2,midi_type,midi_icon)
console = Console()
icons=["dog","cat","mouse","hamster","rabbit","koala","pig","panda_face","cow","bear","lion_face","fox_face","tiger","frog","monkey_face","octopus","dragon_face","unicorn_face","horse","chicken","penguin","bird"]
actions_list=["Show instruments","Select instrument","Add instrument"]
instrument_types_list=["musical_keyboard","drum"]
instruments_list=[]
# main
def main():
	global screen, quit

	cls(screen)
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

		if screen.action in ["Add instrument"]:
			if screen.midi_type is None:
				[(idx,screen.midi_type),screen.message_1]=menu.selector(instrument_types_list,"instrument")
				cls(screen)
				if screen.midi_type in ["quit","q"]:
					quit=True
					cls(screen)
					break

			if screen.midi_type in ["back","b"]:
				screen.action=None
				screen.message_1=None
				screen.message_2=None
				screen.midi_type=None
				cls(screen)

			if screen.midi_type in ['musical_keyboard']:
					[(idx_midi_icon,screen.midi_icon),screen.message_1]=menu.selector(random_icon(icons,instruments_list),"icon")
					cls(screen)
					
					if screen.midi_icon in ["quit","q"]:
						quit=True
						cls(screen)
						break

					if screen.midi_icon in ["back","b"]:
						screen.message_1=None
						screen.message_2=None
						screen.midi_type=None
						screen.midi_icon=None
						cls(screen)

					if screen.midi_icon in icons:
						instruments_list.append(Instrument(screen.midi_type,screen.midi_icon),)
						screen.action=None
						screen.message_1=None
						screen.message_2=None
						screen.midi_type=None
						screen.midi_icon=None
						cls(screen)
			
			if screen.midi_type in ['drum']:
				screen.message_1=f":{random_monkey()}: Instrument :{screen.midi_type}: not yet available. Come back later."
				screen.action=None
				screen.midi_type=None
				cls(screen)

		if screen.action in ["Select instrument"]:
			screen.message_1=f":{random_monkey()}: Action [deep_sky_blue1]{screen.action}[/deep_sky_blue1] not yet available. Come back later."
			screen.action=None
			cls(screen)

		if screen.action in ["Show instruments"]:
			if len(instruments_list)==0:
				screen.message_1=f':{random_monkey()}: No instruments.'
			else:
				screen.message_1=f'Instruments:\n\n'
				for j, instrument in enumerate(instruments_list):
					screen.message_1+=f'{j+1}. :{instrument.midi_type}: :{instrument.midi_icon}:'
					if j<len(instruments_list)-1:
						screen.message_1+=f'\n'
			screen.action=None
			cls(screen)

	return None

# run

if __name__ == "__main__":
	main()

# To run code, save first, then input in Python command line:
# python3 Documents/CodeLibrary/Halcyon/halcyon.py
