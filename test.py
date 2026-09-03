import os
from omnidimension import Client
from dotenv import load_dotenv
load_dotenv()


client = Client(os.environ['OMNIDIM_API_KEY'])
numbers = client.phone_number.list(page=1, page_size=10)
print(numbers)
phone_number_id = numbers['phone_numbers'][0]['id'] 

# api_key = os.environ['OMNIDIM_API_KEY']
# Set OMNIDIM_API_KEY in .env before running this script.
# client = Client(os.environ['OMNIDIM_API_KEY'])
# agents = client.agent.list()
# print(agents)   # each entry has an "id" — that's your OMNIDIM_AGENT_ID

