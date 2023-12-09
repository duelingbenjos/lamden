# Instructions to create a new genesis block from your current node state.

1. SSH into your lamden node.
2. Create / edit the constitution.json file using the nano text editor.
   1. You may need to install nano `apt install nano`
   2.  `rm -rf /root/constitution.json && nano /root/constitution.json`
   3.  Once the nano editor is open, paste the following content: 
   ```json
    {
        "masternodes": {
            "8e63c344863a62d56c16ced5b8f32df5cdfbbea01e63ac5457922c62be7d2e4a" : {},
        }
    }
    ```
    *it doesn't matter what's in the constitution as it'll be overwritten by the constitution in current state. we just need to satisfy the piece of code which checks the shape of the data in constitution.json*
    4. ctrl+x to exit, Y to save.
3. Stop your node. from `/lamden-node-package` `make teardown`
4. Start it up again `make boot`
5. Make sure the output folder is created for the genesis block. And make sure the file does not exist. `mkdir /root/genesis && rm /root/genesis/genesis_block.json`
6. Get into the `lamden_node` container shell : `docker exec -it lamden_node /bin/bash`
7. Run the Python script to create the genesis block : 
   1. `cd lamden/utils`
   2. `python create_genesis.py -k <lamden SK> --output-path /root/genesis/ --migrate filesystem --sp /root/.lamden/`
   3. This will take 10-20 minutes depending on the power of your server.
8. Download the new genesis file to the current working director of your local machine.
   1. From your local machine shell : `scp username@remote:/root/genesis/genesis_block.json ./`
9.  Once downloaded to your local machine :
   1.  Zip it
   2.  Upload it to dropbox.