import os
import json
import ssl
import requests
import asyncio
import subprocess
import paramiko
from fastapi import FastAPI, HTTPException, Query, BackgroundTasks
from pydantic import BaseModel
from pyVim.connect import SmartConnect, Disconnect
from pyVmomi import vim
from requests.auth import HTTPBasicAuth
from dotenv import load_dotenv
import urllib3
from fastapi.middleware.cors import CORSMiddleware
import base64


urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
load_dotenv()

ESXI_HOST = os.getenv('ESXI_HOST')
ESXI_USER = os.getenv('ESXI_USER')
ESXI_PASSWORD = os.getenv('ESXI_PASSWORD')

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], 
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Helper functions
def connect_to_esxi():
    context = ssl._create_unverified_context()
    si = SmartConnect(
        host=ESXI_HOST,
        user=ESXI_USER,
        pwd=ESXI_PASSWORD,
        sslContext=context
    )
    return si


def delete_datastore_file(si, datastore_path, datastore_name):
    try:
        file_manager = si.content.fileManager
        delete_task = file_manager.DeleteDatastoreFile_Task(
            name=f"[{datastore_name}] {datastore_path}",
            datacenter=si.content.rootFolder.childEntity[0]  # Assumes first datacenter
        )
        while delete_task.info.state not in [vim.TaskInfo.State.success, vim.TaskInfo.State.error]:
            pass

        if delete_task.info.state == vim.TaskInfo.State.success:
            print(f"File {datastore_path} deleted successfully.")
        else:
            print(f"Failed to delete file {datastore_path}: {delete_task.info.error.msg}")
    except Exception as e:
        print(f"Error deleting file: {str(e)}")

# Pydantic models for incoming request data
class VMCommand(BaseModel):
    vm_name: str

class CloneCommand(BaseModel):
    source_vm: str
    new_vm: str

# API Endpoints
@app.get("/list")
async def list_vms():
    si = connect_to_esxi()
    try:
        content = si.RetrieveContent()
        vms = content.viewManager.CreateContainerView(
            content.rootFolder, [vim.VirtualMachine], True).view

        vm_status_list = []
        for vm in vms:
            vm_info = {
                "id": vm._moId,
                "name": vm.name,
                "status": "running" if vm.runtime.powerState == vim.VirtualMachinePowerState.poweredOn else "stopped",
                "os": vm.config.guestFullName if vm.config else "Unknown OS",
                "memory": f"{vm.config.hardware.memoryMB // 1024}GB" if vm.config else "Unknown",
                "cpu": f"{vm.config.hardware.numCPU} vCPUs" if vm.config else "Unknown",
                "ip_address": vm.guest.ipAddress if vm.guest and vm.guest.ipAddress else "N/A"
            }
            vm_status_list.append(vm_info)

        return {"vms": vm_status_list}
    finally:
        Disconnect(si)


@app.get("/start")
async def start_vm( vm_name: str = Query(...)):
    si = connect_to_esxi()
    try:
        content = si.RetrieveContent()
        vm = next((vm for vm in content.viewManager.CreateContainerView(
            content.rootFolder, [vim.VirtualMachine], True).view if vm.name == vm_name), None)
        if vm:
            if vm.runtime.powerState == 'poweredOff':
                vm.PowerOn()  # Start VM
                return {"message": f"Starting VM: {vm_name}"}
            else:
                return {"message": f"VM {vm_name} is already running."}
        else:
            raise HTTPException(status_code=404, detail=f"VM {vm_name} not found.")
    finally:
        Disconnect(si)

@app.get("/stop")
async def stop_vm( vm_name: str = Query(...)):
    si = connect_to_esxi()
    try:
        content = si.RetrieveContent()
        vm = next((vm for vm in content.viewManager.CreateContainerView(
            content.rootFolder, [vim.VirtualMachine], True).view if vm.name == vm_name), None)
        if vm:
            if vm.runtime.powerState == 'poweredOn':
                vm.PowerOff()  # Stop VM
                return {"message": f"Stopping VM: {vm_name}"}
            else:
                return {"message": f"VM {vm_name} is already stopped."}
        else:
            raise HTTPException(status_code=404, detail=f"VM {vm_name} not found.")
    finally:
        Disconnect(si)

@app.get("/reset")
async def reset_vm( vm_name: str = Query(...)):
    si = connect_to_esxi()
    try:
        content = si.RetrieveContent()
        vm = next((vm for vm in content.viewManager.CreateContainerView(
            content.rootFolder, [vim.VirtualMachine], True).view if vm.name == vm_name), None)
        if vm:
            if vm.runtime.powerState == 'poweredOn':
                vm.ResetVM_Task()  # Reset VM
                return {"message": f"Resetting VM: {vm_name}"}
            else:
                return {"message": f"VM {vm_name} is not running, so it cannot be reset."}
        else:
            raise HTTPException(status_code=404, detail=f"VM {vm_name} not found.")
    finally:
        Disconnect(si)

@app.get("/screenshot")
async def screenshot_vm(vm_name: str = Query(...)):
    try:
        si = connect_to_esxi()
        content = si.RetrieveContent()
        vm = next((vm for vm in content.viewManager.CreateContainerView(
            content.rootFolder, [vim.VirtualMachine], True).view if vm.name == vm_name), None)

        if vm:
            if vm.runtime.powerState == 'poweredOn':
                task = vm.CreateScreenshot_Task()
                while task.info.state not in [vim.TaskInfo.State.success, vim.TaskInfo.State.error]:
                    pass

                if task.info.state == vim.TaskInfo.State.success:
                    screenshot_result = task.info.result
                    if screenshot_result.startswith("["):
                        datastore_path = screenshot_result.split('] ')[-1]
                        datastore_name = screenshot_result.split('[')[-1].split(']')[0]
                        download_url = f"https://{ESXI_HOST}/folder/{datastore_path}?dcPath=ha-datacenter&dsName={datastore_name}"
                        download_url = download_url.replace(' ', '%20')

                        response = requests.get(download_url, auth=HTTPBasicAuth(ESXI_USER, ESXI_PASSWORD), verify=False)
                        if response.status_code == 200:
                            image_base64 = base64.b64encode(response.content).decode("utf-8")
                            delete_datastore_file(si, datastore_path, datastore_name)
                            return {"message": "Screenshot taken successfully.", "screenshot": image_base64}
                        else:
                            raise HTTPException(status_code=500, detail="Failed to download the screenshot.")
                    else:
                        raise HTTPException(status_code=500, detail="Unexpected screenshot data format.")
                else:
                    raise HTTPException(status_code=500, detail="Screenshot task failed.")
            else:
                return {"message": f"VM {vm_name} is not running."}
        else:
            raise HTTPException(status_code=404, detail=f"VM {vm_name} not found.")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error: {str(e)}")
    finally:
        Disconnect(si)

@app.post("/clone")
async def clone_vm(command: CloneCommand, background_tasks: BackgroundTasks):
    source_vm_name, new_vm_name = command.source_vm, command.new_vm
    si = connect_to_esxi()

    try:
        content = si.RetrieveContent()
        vm = next((vm for vm in content.viewManager.CreateContainerView(
            content.rootFolder, [vim.VirtualMachine], True).view if vm.name == source_vm_name), None)

        if not vm:
            raise HTTPException(status_code=404, detail=f"Source VM '{source_vm_name}' not found.")

        if vm.runtime.powerState == vim.VirtualMachinePowerState.poweredOn:
            raise HTTPException(status_code=400, detail=f"VM '{source_vm_name}' is running. Please power it off before cloning.")

        background_tasks.add_task(run_clone_process, source_vm_name, new_vm_name)
        return {"message": f"Cloning of VM '{new_vm_name}' from '{source_vm_name}' has started in the background."}

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"ERROR: {str(e)}")
    finally:
        Disconnect(si)


def run_clone_process(source_vm_name, new_vm_name):
    si = connect_to_esxi()
    try:
        content = si.RetrieveContent()
        vm = next((vm for vm in content.viewManager.CreateContainerView(
            content.rootFolder, [vim.VirtualMachine], True).view if vm.name == source_vm_name), None)
        if not vm:
            print(f"ERROR: Source VM '{source_vm_name}' not found.")
            return

        source_vm_path = vm.config.files.vmPathName
        source_datastore_name, source_vm_folder = source_vm_path.strip("[]").split("] ")
        source_vm_folder = source_vm_folder.split("/")[0]

        datastores = content.viewManager.CreateContainerView(
            content.rootFolder, [vim.Datastore], True).view
        if not datastores:
            print("ERROR: No datastores found.")
            return

        best_datastore = max(datastores, key=lambda ds: ds.summary.freeSpace)
        destination_datastore_name = best_datastore.summary.name

        ssh = paramiko.SSHClient()
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        ssh.connect(ESXI_HOST, username=ESXI_USER, password=ESXI_PASSWORD)

        steps = [
            ("Creating VM directory", f"mkdir /vmfs/volumes/{destination_datastore_name}/{new_vm_name}"),
            (
                "Cloning disk",
                f"vmkfstools -i /vmfs/volumes/{source_datastore_name}/{source_vm_folder}/{source_vm_name}.vmdk "
                f"-d thin /vmfs/volumes/{destination_datastore_name}/{new_vm_name}/{new_vm_name}.vmdk"
            ),
            (
                "Copying VMX file",
                f"cp /vmfs/volumes/{source_datastore_name}/{source_vm_folder}/{source_vm_name}.vmx "
                f"/vmfs/volumes/{destination_datastore_name}/{new_vm_name}/{new_vm_name}.vmx"
            ),
            (
                "Updating display name",
                f"sed -i 's/displayName = \"{source_vm_name}\"/displayName = \"{new_vm_name}\"/g' "
                f"/vmfs/volumes/{destination_datastore_name}/{new_vm_name}/{new_vm_name}.vmx"
            ),
            (
                "Updating VM disk reference",
                f"sed -i 's/{source_vm_name}.vmdk/{new_vm_name}.vmdk/g' "
                f"/vmfs/volumes/{destination_datastore_name}/{new_vm_name}/{new_vm_name}.vmx"
            ),
            (
                "Ensuring unique UUID",
                f"sed -i '/uuid.bios/d' /vmfs/volumes/{destination_datastore_name}/{new_vm_name}/{new_vm_name}.vmx && "
                f"echo 'uuid.action = \"create\"' >> /vmfs/volumes/{destination_datastore_name}/{new_vm_name}/{new_vm_name}.vmx"
            ),
            (
                "Registering VM",
                f"vim-cmd solo/registervm /vmfs/volumes/{destination_datastore_name}/{new_vm_name}/{new_vm_name}.vmx"
            )
        ]

        for description, command_line in steps:
            stdin, stdout, stderr = ssh.exec_command(command_line)
            exit_status = stdout.channel.recv_exit_status()
            if exit_status != 0:
                error = stderr.read().decode().strip()
                ssh.close()
                print(f"ERROR during '{description}': {error}")
                return

        ssh.close()
        print(f"Clone '{new_vm_name}' created successfully from '{source_vm_name}'")
    except Exception as e:
        print(f"ERROR: Failed to clone VM: {str(e)}")
    finally:
        Disconnect(si)


@app.get("/delete")
async def delete_vm( vm_name: str = Query(...)):
    si = connect_to_esxi()
    try:
        content = si.RetrieveContent()
        vm = next((vm for vm in content.viewManager.CreateContainerView(
            content.rootFolder, [vim.VirtualMachine], True).view if vm.name == vm_name), None)
        if not vm:
            raise HTTPException(status_code=404, detail=f"VM '{vm_name}' not found.")
        
        if vm.runtime.powerState == vim.VirtualMachinePowerState.poweredOn:
            task = vm.PowerOff()
            while task.info.state not in [vim.TaskInfo.State.success, vim.TaskInfo.State.error]:
                pass
        
        vm_path = vm.config.files.vmPathName  # Example: "[datastore1] VM_NAME/VM_NAME.vmx"
        datastore_name, vm_folder = vm_path.strip("[]").split("] ")
        vm_folder = os.path.dirname(vm_folder)
        
        vm.UnregisterVM()
        content = si.RetrieveContent()
        datastore = next((ds for ds in content.viewManager.CreateContainerView(
            content.rootFolder, [vim.Datastore], True).view if ds.name == datastore_name), None)
        
        datacenter = next((dc for dc in content.rootFolder.childEntity if isinstance(dc, vim.Datacenter)), None)
        if not datacenter:
            raise HTTPException(status_code=500, detail="No datacenter found.")
        
        if datastore:
            file_manager = content.fileManager
            delete_task = file_manager.DeleteDatastoreFile_Task(
                name=f"[{datastore_name}] {vm_folder}",
                datacenter=datacenter
            )
            while delete_task.info.state not in [vim.TaskInfo.State.success, vim.TaskInfo.State.error]:
                pass

            if delete_task.info.state == vim.TaskInfo.State.success:
                return {"message": f"VM storage '{vm_folder}' deleted from datastore '{datastore_name}'."}
            else:
                raise HTTPException(status_code=500, detail=f"Failed to delete VM storage '{vm_folder}'.")
        else:
            raise HTTPException(status_code=404, detail=f"Datastore '{datastore_name}' not found.")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"ERROR: Failed to delete VM: {str(e)}")
    finally:
        Disconnect(si)
