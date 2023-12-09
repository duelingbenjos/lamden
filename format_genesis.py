import json

# Open your file
with open('genesis_block.json', 'r') as f:
    data = json.load(f)

# Now data is a Python dictionary, you can pretty print it
pretty_data = json.dumps(data, indent=4)

# If you want to write the pretty data back into the file
with open('formatted_genesis.json', 'w') as f:
    f.write(pretty_data)