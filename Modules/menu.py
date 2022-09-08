# Project Halcyon - Synth
# menu.py
# authors: pablo.mendoza

# libraries
import sys
import pandas as pd
import random
from rich import print, pretty
pretty.install()
quit=False

# functions

def random_monkey():
	monkeys=["see_no_evil","speak_no_evil","hear_no_evil"]
	monkey=monkeys[random.randint(0,2)]
	return monkey

def selector(menu_items,menu):
	menu_list=["action","scrpit","midi"]
	error=None
	df = pd.DataFrame(menu_items, columns=["action"])
	menu_items=df["action"]
	match=False
	selection=None
	while not match:
		print(f"\n Choose {menu}:")
		for idx, item in menu_items.iteritems():
			if menu in ["instrument","icon"]:
				print (f"\n {idx+1}. :{item}:")
			else:
				print (f"\n {idx+1}. {item}")
		answer=input("\n ▶ ")
		if  answer.lower() in ["quit","q","back","b"]:
			result=answer.lower()
			match=True
		else:
			try:
				try:
					selection=int(answer)-1
				except:
					for idx, item in menu_items.iteritems():
						if answer.lower() in {item.lower()}:
							selection=idx
				current_item={"current action": [menu_items[selection]]
								}
				current_item=pd.DataFrame(current_item,columns=["current action"])
				result=current_item["current action"].values[0]
				match=True
			except:
				error=f":{random_monkey()}: Invalid {menu}, try again."
				result=None
				match=True
	return [(selection,result),error]

