Christian's **Fedora Atomic** Setup Guide
=========================================

This guide provides the steps required to set up a system using **Fedora Atomic** with VFIO (Virtual Function I/O) support for GPU passthrough, NVIDIA drivers, libvirt setup, disk management, and virtual machine (VM) tuning. Each section includes the necessary commands and explanations for configuration.

* * *

### Setup VFIO for GPU Passthrough

To use GPU passthrough on your Silverblue system, follow these steps:

#### Bind VFIO-PCI Driver to GPU

1.  First, find the IDs for your GPU and GPU Audio using the command:
    
        lspci -nnk
    
    This will list all PCI devices and their IDs. Note down the IDs for your GPU and GPU Audio.
    
2.  Use the following command to bind the `vfio-pci` driver to your GPU. Replace the IDs with those you noted from the previous step:
    
        rpm-ostree kargs --append-if-missing=rd.driver.pre=vfio_pci --append-if-missing=amd_iommu=on --append-if-missing=iommu=pt --append-if-missing="vfio-pci.ids=10de:2860,10de:22bd"
    
3.  If you are using proprietary NVIDIA drivers, add the following additional parameters:
    
        --append-if-missing=video=efifb:off --append-if-missing=rd.driver.blacklist=nouveau --append-if-missing=modprobe.blacklist=nouveau --append-if-missing=nvidia-drm.modeset=1
    

#### Update the Initramfs

To ensure the VFIO driver is included in the initramfs (initial RAM filesystem), run the following:

    rpm-ostree initramfs --enable --arg="--add-drivers" --arg="vfio-pci"

### Install VM Packages

To manage virtual machines, you'll need the following packages:

    rpm-ostree install qemu-kvm-core libvirt virt-manager samba 

### Install NVIDIA Drivers (Optional)

If you are using an NVIDIA GPU, you may want to install proprietary NVIDIA drivers. Follow these steps:

1.  Add RPM Fusion repositories for both free and non-free software:
    
        rpm-ostree install --apply-live https://mirrors.rpmfusion.org/free/fedora/rpmfusion-free-release-$(rpm -E %fedora).noarch.rpm https://mirrors.rpmfusion.org/nonfree/fedora/rpmfusion-nonfree-release-$(rpm -E %fedora).noarch.rpm
    
2.  Update the repositories:
    
        rpm-ostree update --uninstall rpmfusion-free-release-$(rpm -E %fedora)-1.noarch --uninstall rpmfusion-nonfree-release-$(rpm -E %fedora)-1.noarch --install rpmfusion-free-release --install rpmfusion-nonfree-release
    
3.  Install NVIDIA-related packages:
    
        rpm-ostree install akmod-nvidia xorg-x11-drv-nvidia-cuda xorg-x11-drv-nvidia-power
    

#### Reboot

Before proceeding with additional configuration, reboot your system to apply changes.

#### NVIDIA Continued (Optional)

Enable and mask NVIDIA services:

    sudo systemctl enable nvidia-{suspend,resume,hibernate}

### Setup Libvirt Hooks

1.  Create the directory for Libvirt hooks:
    
        mkdir -p /etc/libvirt/hooks
    
2.  Navigate to the hooks directory and run the hook update script:
    
        cd /to/hooks/folder
    
3.  Enable the `libvirtd` service to start automatically:
    
        sudo systemctl enable --now libvirtd
    

### Add User to the Libvirt Group

1.  Switch to the root user:
    
        sudo su
    
2.  Add the current user to the `libvirt` group by editing the `/etc/group` file:
    
        grep -E '^libvirt:' /usr/lib/group >> /etc/group
    
3.  Exit from the root session:
    
        exit
    
4.  Add your user to the `libvirt` group:
    
        sudo usermod -aG libvirt $USER
    

### Auto Decrypt and Mount Disks, Replace Home Directory

#### Set Root Account Password

1.  Switch to the root user and set a password:
    
        sudo su
    
2.  Log out and log in as root.
    

#### Enable Automatic Decrypting

1.  To enable automatic decryption for encrypted partitions, you can either use **GNOME Disks** or do it manually by creating a keyfile:
    
        head -c 512 /dev/random > /etc/uuid.key
    
2.  Edit the `/etc/crypttab` file to auto-decrypt at boot:
    
        echo "luks-uuid UUID=uuid /etc/uuid.key luks,nofail" >> /etc/crypttab
    

#### Create Folders and Mount Primary Storage

1.  Create a folder for mounting the storage drive/partition:
    
        mkdir -p /var/mnt/DATAONE
    
2.  Mount the partition either through **GNOME Disks**, **KDE Partition Manager**, **GParted**, or manually by editing `/etc/fstab`.
    

#### Backup Original Home Directory

Backup your current home directory to prevent data loss:

    rsync -av /var/home/christian /var/home/.christianorig

#### Bind Mount Home Directory

1.  Remove the existing home directory:
    
        rm -rf /var/home/christian
    
2.  Create a new directory:
    
        mkdir -p /var/home/christian
    
3.  Edit `/etc/fstab` to bind mount the home directory from the primary storage partition:
    
        echo "/var/mnt/DATAONE/christian /var/home/christian none bind,nofail,x-systemd.device-timeout=2  0 0" >> /etc/fstab
    

#### Restore SELinux Contexts and Fix Permissions

1.  Restore SELinux contexts:
    
        restorecon -R /var/home/christian
    
2.  Change ownership of the directories:
    
        chown -R christian:christian /var/home/christian
    

#### Mount All Filesystems

Test the mount:

    mount -a

Log out and log in as your user (`christian`).

### Shared Hotspot And VM Network Setup

#### Add the bridge connection profile
    sudo nmcli con add type bridge ifname br-hotspot con-name br-hotspot autoconnect yes
    
#### Configure the bridge IP address
    sudo nmcli con modify br-hotspot ipv4.method manual ipv4.addresses 192.168.42.1/24 #No gateway needed on the bridge itself
    
#### Disable Spanning Tree Protocol (usually not needed for a simple setup)
    sudo nmcli con modify br-hotspot bridge.stp no
    
#### Bring the connection up (might already be up due to autoconnect)
    sudo nmcli con up br-hotspot
    
#### Configure firewalld to allow DHCP and DNS traffic coming in on the br-hotspot interface, destined for the dnsmasq server running locally
##### 0. Create hotspot zone
    sudo firewall-cmd --permanent --new-zone=internal
##### 1. Assign bridge interface to the internal zone (if not already done)
    sudo firewall-cmd --permanent --zone=internal --add-interface=br-hotspot
##### 2. Allow DHCP service in the 'internal' zone 
    sudo firewall-cmd --permanent --zone=internal --add-service=dhcp 
##### 3. Allow DNS service in the 'internal' zone
    sudo firewall-cmd --permanent --zone=internal --add-service=dns 
##### 4. Reload firewalld to apply changes
    sudo firewall-cmd --reload 
##### 5. Verify (optional) 
    sudo firewall-cmd --zone=hotspot --list-all
    

#### Enable Internet Access On Bridge And Hotspot
	
##### Enable Kernel IP Forwarding (Permanent):
###### Create or modify a sysctl configuration file for persistence
	echo "net.ipv4.ip_forward = 1" | sudo tee /etc/sysctl.d/99-forwarding.conf
###### Apply the setting immediately without rebooting
	sudo sysctl -p /etc/sysctl.d/99-forwarding.conf # You can verify with: sysctl net.ipv4.ip_forward	

##### Assign Interfaces and Sources to firewalld Zones:
###### Assign the internet interface to the 'external' zone
	sudo firewall-cmd --permanent --zone=external --change-interface=wlp4s0
###### Assign the hotspot bridge interface to the internal zone 
	sudo firewall-cmd --permanent --zone=internal --change-interface=br-hotspot 
###### Optional: Explicitly assign the source subnet to internal zone 
	sudo firewall-cmd --permanent --zone=internal -–add-source=192.168.42.0/24

###### Set External Zone as Default Zone and enable services available to previous default
	sudo firewall-cmd –set-default-zone=external
	sudo firewall-cmd --zone=external --add-service=samba-client --add-service=dhcpv6-client --permanent
###### Enable Masquerading (NAT) on the External Zone:**`
	sudo firewall-cmd --permanent --zone=external --add-masquerade 

##### Create Policy to Allow Forwarding from internal to external
###### Create the policy object 
	sudo firewall-cmd --permanent --new-policy PInt2Ext 
###### Define traffic source zone (ingress) 
	sudo firewall-cmd --permanent --policy PInt2Ext --add-ingress-zone=internal 
###### Define traffic destination zone (egress) 
	sudo firewall-cmd --permanent --policy PInt2Ext --add-egress-zone=external 
###### Set the policy action to allow the traffic 
	sudo firewall-cmd --permanent --policy PInt2Ext --set-target ACCEPT
	
###### Apply All firewalld Changes
	sudo firewall-cmd --reload

###### Copy the hotspot script to a directory in the path to be used in the terminal
	cd /var/mnt/DATAONE/Tools/Hotspot
	sudo ./updatehotspot

1.  **Create a libvirt network** **using the following XML**:
    
    XML
    ```xml
    <network>
      <name>hotspot-bridged-net</name> 
      <forward mode='bridge'/>
      <bridge name='br-hotspot'/>
    </network>
    ```
    
`**Add the network to your VM**` 

`Under "Network source", choose "Virtual network 'hotspot-bridged-net': Bridge``d Network``"`

#### Troubleshooting

`No Internet In VM when hotspot not launched: Due to dnsmasq for the bridge not being launched, either launch it or create a static IP in the VM on the same subnet as the bridge.`

`No Internet In VM OR Hotspot: Check firewalld or iptables to see if the traffic from the bridge is being natted via the interface facing interface.`

`Misc: Ensure the bridge is created, and check that any services uses the network interfaces are running or that the network interfaces required by them haven’t changed names.`

### Flatpak Setup

#### Enable Flathub User Repo

To install Flatpaks, add the Flathub repository:

    flatpak --user remote-add --if-not-exists flathub https://dl.flathub.org/repo/flathub.flatpakrepo

#### Install Firefox via Flatpak (Optional)

If you don’t have Firefox installed yet, install it with the following command:

    flatpak install --user flathub org.mozilla.firefox flathub org.freedesktop.Platform.ffmpeg-full

### `Setup Samba for sharing files`

### 1\. **Set Samba User Password**

Start by adding the Samba user with a password. This user will be used for authentication when accessing shared files.

`sudo smbpasswd -a christian`

### 2\. **Configure Samba Settings**

Next, modify the Samba configuration file (`/etc/samba/smb.conf`) to define global settings and shared directories. Ensure to also set Samba to bind to the Libvirt interface, you can check the network interfaces on your system by running:

ip a

1.  Open the `smb.conf` file for editing:
    

`sudo nano /etc/samba/smb.conf`

2.  Add the following configuration to the file:
    

`[global]`
    `workgroup = SAMBA`
    `security = user`
    `passdb backend = tdbsam`
    `bind interfaces only = yes`
    `interfaces = virbr0,br-hotspot`
    `force user = christian`

`[Shared]`
    `path = /var/mnt/DATAONE/Shared`
    `browseable = no`
    `read only = no`
    `create mask = 0755` 
    `directory mask = 0775`
    `valid users = christian`

`[Creative]`
    `path = /var/mnt/DATAONE/Creative`
    `browseable = no`
    `read only = no`
    `create mask = 0755` 
    `directory mask = 0775`
    `valid users = christian`

### 3\. **Enable SELinux for Shared Folders**

If the shared folders are inside your home directory, you need to enable SELinux rules to allow Samba to access them.

`sudo setsebool -P samba_enable_home_dirs on`
`sudo setsebool -P samba_export_all_rw=1`
`sudo setsebool -P samba_export_all_ro=1`
`sudo setsebool -P samba_share_fusefs=1`
`sudo setsebool -P virt_use_samba=1`

### 4\. **Add Firewall Exceptions for Samba**

Ensure that the firewall allows Samba traffic. Add an exception for Samba service to your firewall configuration.

1.  Add the firewall exception:
    

`sudo firewall-cmd --zone=``internal` `--add-service=samba –permanent`
`sudo firewall-cmd --zone=``libvirt` `--add-service=samba --permanent`

2.  Reload the firewall configuration:
    

`sudo firewall-cmd --reload`

### 5\. **Start Samba Service**

Finally, start the Samba service to enable file sharing.

`sudo systemctl start smb.service`

### 6\. **Troubleshooting: Disable SELinux Temporarily (If Needed)**

If you're unable to connect to the shared folders from a VM, SELinux may be causing the issue. You can temporarily disable SELinux to test this:

`sudo setenforce 0`

If this resolves the issue, you may need to adjust SELinux policies for a permanent solution.

### Setup Looking Glass

#### Add IVSHMEM Device to VM XML

In your VM's XML configuration, add the following `shmem` device for **Looking Glass**:

    <devices>

To determine the appropriate size for your screen, use this formula:

1.  **Calculate total bytes**:
    
        w * h * 4 * 2
    
2.  **Convert to MiB**:
    
        total bytes / 1024 / 1024 = total MiB + 10
    

For a 1080p screen:

    1920 * 1080 * 4 * 2 = 16,588,800 bytes

#### Create Shared Memory Block on Host

1.  Create a temporary file for the shared memory:
    
        echo "#Type Path               Mode UID  GID Age Argument" >> /etc/tmpfiles.d/10-looking-glass.conf
    
2.  Create the temporary files immediately:
    
        sudo systemd-tmpfiles --create /etc/tmpfiles.d/10-looking-glass.conf
    

#### Install Looking Glass Host in VM

Follow the Looking Glass documentation to install the **Looking Glass Host** in the VM and build the **Looking Glass Client** for the host.

### VM Tuning

#### CPU Pinning

To pin specific virtual CPUs to physical cores, use the following in your VM XML:

    <vcpu placement="static">

#### Add CPU Cache and Topology

Add this to the `<topology>` section:

    <cache mode="passthrough"/>

#### ACPITable and Overcommit

Add the following `<qemu:commandline>` entries:

    <qemu:commandline>

To avoid permissions errors, add the `acpitable.bin` as a CD-ROM to your VM.

#### Hyper-V Settings for VM

Add the following to the `<hyperv>` section of the XML:

    <hyperv>

### Enable running Games via Network Share

#### For GOG Games

EnableLinkedConnections registry value enables Windows Vista, Windows 7, Windows 8, Windows 8.1, Windows 10 or later to share network connections between the filtered access token and the full administrator access token for a member of the Administrators group. After you configure this registry value, LSA checks whether there is another access token that is associated with the current user session if a network resource is mapped to an access token. If LSA determines that there is a linked access token, it adds the network share to the linked location.  
  
1\. Run Registry Editor (regedit).  
2\. Navigation to the following registry key:  
HKEY\_LOCAL\_MACHINE\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\Policies\\System  
3\. Right click on System, then point to New, and then click DWORD (32-bit) Value.  
4\. Type EnableLinkedConnections, and then press ENTER.  
5\. Right-click EnableLinkedConnections, and then click Modify.  
6\. In the Value data box, type 1, and then click OK.  
7\. Restart the computer.

#### For EA Games/Ubisoft/Etc.

EA uses a Background service called "EABackgroundService" - This runs as a System service, and not the logged-in user.

As the default user, creation of network files are only accessible via the user who generated the Mapping - EA's background service cannot see the mapped drive. As such we have to create it as the SYSTEM user.

**Step one:** Create a BAT script with the following command

`net use z:` `[\\servername\sharedfolder](//servername/sharedfolder)` `/user:username password`

`**Step two:**` `Add the bat script to run at startup`

*   `Open the Local Group Policy Editor.`
    

*   In the console tree, click **Scripts (Startup/Shutdown)** . The path is **Computer Configuration\\Windows Settings\\Scripts (Startup/Shutdown)** .
    

*   In the results pane, double-click **Startup** .
    

*   In the **Startup Properties** dialog box, click **Add** and select the BAT script from the previous step.
    

**Step three**: Restart the VM.

**WARNING**: You can only remove this mapping the same way you created it, from the SYSTEM account. If you need to remove it, follow steps 1 and 2 but change the command to `net use z: /delete`.

**NOTE**: The newly created mapped drive will now appear for ALL users of this system but they will see it displayed as "Disconnected Network Drive (Z:)". Do not let the name fool you. It may claim to be disconnected but it will work for everyone.

### Additional Tweaks

For more optimizations and tweaks, refer to the `qemu` script in the hooks directory.

### Manual Configuration For Shared Network With Hotspot for VM (EXTRA DOCUMENTATION, ALREADY COVERED IN MAIN SECTION)

Okay, let's break down how to achieve this setup. The core idea is to create a separate network for your hotspot clients and the VM, bridged together, and then route traffic from this network to the internet via your host's primary Wi-Fi connection (`wlan0`). We'll use the USB Wi-Fi adapter (`wlan1`) for the hotspot.

You are correct that bridging a standard Wi-Fi client interface (`wlan0` in your case) is generally not possible due to 802.11 protocol limitations. However, bridging a Wi-Fi interface operating in Access Point (AP) mode (`wlan1`) _is_ possible and is the standard way to connect AP clients to a wired (or in this case, virtual) network. Macvtap in bridge mode wouldn't work with `wlan0` either for the same reasons.

Using a libvirt _routed_ network would isolate the VM on its own subnet, requiring routing between the hotspot clients' subnet and the VM's subnet. While possible, it adds complexity and might interfere with device discovery protocols. A _bridged_ setup where the hotspot clients and the VM share the same Layer 2 network is simpler and directly meets your requirement for seamless access without port forwarding.

We will create a bridge interface on the host, add the USB Wi-Fi card (in AP mode) to this bridge, and then configure the libvirt network to use this existing host bridge. We will use `iproute2` or `nmcli` for bridge management instead of `bridge-utils`.

**Prerequisites:**

1.  **Identify Interfaces:** Confirm your PCIe Wi-Fi is `wlan0` and your USB Wi-Fi is `wlan1`. Use `iw dev` or `ip link` to verify.
    
2.  **Check USB Wi-Fi Capabilities:** Ensure `wlan1` supports AP (Access Point) mode and 5GHz. Run `iw list` and look under the section for your USB adapter (`wlan1`). Check for `Supported interface modes:` (should include `AP`) and `Band 2:` (should list 5GHz channels/frequencies).
    
3.  **Install Necessary Software:**
    
    Bash
    
    `# Debian/Ubuntu based`
    `sudo apt update`
    `sudo apt install hostapd dnsmasq`
    
    `# Fedora/RHEL based`
    `sudo dnf update`
    `sudo dnf install hostapd dnsmasq`
    
    _(Note:_ _`iptables-persistent`__/__`iptables-services`_ _is for saving firewall rules across reboots)._
    

**Steps:**

**Step 1: Create a Network Bridge on the Host**

We'll create a bridge interface (e.g., `br-hotspot`) that will connect the hotspot interface (`wlan1`) and the VM's virtual NIC.

*   **Using** **`iproute2`****:**
    
    Bash
    
    `# Create the bridge`
    `sudo ip link add name br-hotspot type bridge`
    `# Bring the bridge up`
    `sudo ip link set br-hotspot up`
    `# Assign a static IP to the bridge (this will be the gateway for hotspot clients and VM)`
    `# Choose a subnet not used elsewhere, e.g., 192.168.42.0/24`
    `sudo ip addr add 192.168.``42``.1/24 dev br-hotspot`
    
    _(To make_ _`iproute2`_ _changes persistent, you'd typically use your distribution's network configuration files, e.g.,_ _`/etc/network/interfaces`_ _on Debian/Ubuntu or_ _`/etc/sysconfig/network-scripts/`_ _on older RHEL/Fedora, or use NetworkManager)._
    
*   **Using** **`NetworkManager`** **(Often preferred on desktops/laptops):**
    
    Bash
    
    `# Add the bridge connection profile`
    `sudo nmcli con add type bridge ifname br-hotspot con-name br-hotspot autoconnect yes`
    `# Configure the bridge IP address`
    `sudo nmcli con modify br-hotspot ipv4.method manual ipv4.addresses 192.168.``42``.1/24  # No gateway needed on the bridge itself`
    `# Disable Spanning Tree Protocol (usually not needed for a simple setup)`
    `sudo nmcli con modify br-hotspot bridge.stp no`
    `# Bring the connection up (might already be up due to autoconnect)`
    `sudo nmcli con up br-hotspot`
    
    _Make sure_ _`wlan1`_ _is not automatically managed by NetworkManager in client mode if you plan to use_ _`hostapd`_ _manually. You might need to tell NetworkManager to ignore it (__`unmanaged-devices`__) or configure it specifically for AP mode within NetworkManager._
    

**Step 2: Configure the Hotspot (hostapd + dnsmasq)**

These tools will manage the Wi-Fi AP (`hostapd`) and provide DHCP/DNS services (`dnsmasq`) to clients connecting to the hotspot (and the VM via the bridge).

1.  **Configure** **`hostapd`****:** Create `/etc/hostapd/hostapd.conf` (adjust path if needed).
    
    Ini, TOML
    
    `# Interface to use and bridge to attach it to`
    `interface=wlan1`
    `bridge=br-hotspot`
    `driver=nl80211 # Standard Linux driver`
    
    `# Basic AP settings`
    `ssid=Your_5GHz_Hotspot # Choose your network name`
    `hw_mode=a          # 'a' for 5GHz (802.11a/n/ac)`
    `channel=44         # Choose a free 5GHz channel (e.g., 36, 40, 44, 48, etc. Check 'iw list')`
    `ieee80211n=1       # Enable 802.11n`
    `ieee80211ac=1      # Enable 802.11ac (if card supports it)`
    `wmm_enabled=1      # Required for N/AC speeds`
    
    `# Security (WPA2-PSK - Recommended)`
    `macaddr_acl=0      # Accept all MAC addresses`
    `auth_algs=1        # WPA2-PSK`
    `ignore_broadcast_ssid=0 # Make SSID visible`
    `wpa=2`
    `wpa_passphrase=YourSecurePassword # Choose a strong password (at least 8 characters)`
    `wpa_key_mgmt=WPA-PSK`
    `rsn_pairwise=CCMP  # Use CCMP (AES) for WPA2`
    `# wpa_pairwise=TKIP CCMP # Avoid TKIP if possible, CCMP is more secure`
    
    *   **Important:** Ensure `wlan1` is not being actively managed by NetworkManager or other network services when `hostapd` tries to use it. If using NetworkManager, configuring the AP mode _within_ NetworkManager might be an alternative, but `hostapd` often offers more control.
        
2.  **Configure** **`dnsmasq`****:** Create a config file, e.g., `/etc/dnsmasq.d/hotspot.conf`.
    
    Ini, TOML
    
    `# Listen only on the bridge interface`
    `interface=br-hotspot`
    `bind-interfaces # Important for security/correctness`
    
    `# DHCP range for clients (must be in the same subnet as the bridge IP)`
    `# Format: start_ip, end_ip, subnet_mask, lease_time`
    `dhcp-range=192.168.``42``.100,192.168.``42``.200,255.255.255.0,12h`
    
    `# Provide the bridge IP as the gateway (router)`
    `dhcp-option=option:router,192.168.``42``.1`
    
    `# Provide DNS servers (can use the host itself, or public DNS)`
    `dhcp-option=option:dns-server,192.168.``42``.1 # Use dnsmasq itself for DNS relay/caching`
    `# dhcp-option=option:dns-server,8.8.8.8,1.1.1.1 # Or use Google/Cloudflare DNS`
    
    `# Optional: Log DHCP actions for debugging`
    `# log-dhcp`
    
3.  **Start and Enable Services:**
    
    Bash
    
    `sudo systemctl stop systemd-resolved # Often conflicts with dnsmasq on port 53`
    `sudo systemctl disable systemd-resolved # Optional: disable permanently if using dnsmasq fully`
    
    `sudo systemctl start hostapd`
    `sudo systemctl start dnsmasq`
    `sudo systemctl enable hostapd # Start on boot`
    `sudo systemctl enable dnsmasq # Start on boot`
    
    _(Check service status with_ _`systemctl status hostapd dnsmasq`__)_
    

**Step 3: Enable IP Forwarding and NAT on the Host**

This allows devices connected to `br-hotspot` (hotspot clients and the VM) to reach the internet via your host's primary internet connection (`wlan0`).

1.  **O****pen the dnsmasq and dns ports in firewalld****:**
    
    Bash
    
    `#0. Create hotspot zone`
    `_sudo firewall-cmd --permanent --new-zone=hotspot_`
    `# 1. Assign bridge interface to the '``hotspot``' zone (if not already done)` 
    `_sudo firewall-cmd --permanent --zone=_``_hotspot_` `_--add-interface=br-hotspot_` 
    `# 2. Allow DHCP service in the 'internal' zone` 
    `_sudo firewall-cmd --permanent --zone=_``_hotspot_` `_--add-service=dhcp_` 
    `# 3. Allow DNS service in the 'internal' zone` 
    `_sudo firewall-cmd --permanent --zone=_``_hotspot_` `_--add-service=dns_` 
    `# 4. Reload firewalld to apply changes` 
    `_sudo firewall-cmd --reload_` 
    `# 5. Verify (optional)` 
    `_sudo firewall-cmd --zone=hotspot --list-all_`
    
2.  **Set up NAT (Masquerading):**
    
    Bash
    
    `# This rule tells iptables to rewrite the source IP for traffic going out  wlp4s0`
    `# from the br-hotspot network, making it look like it came from the host.`
    `sudo iptables -t nat -A POSTROUTING -s 192.168.42.0/24 -o wlp4s0 -j MASQUERADE`
    
    `O``n systems like Fedora Silverblue, using` `iptables` `directly, especially via the` `firewalld` `direct interface, is discouraged and deprecated. The goal is to manage firewall rules entirely through` `firewalld``'s native features like zones, services, ports, and rich rules.`
    

	
	`**Enable Kernel IP Forwarding (Permanent):**`
	`**# Create or modify a sysctl configuration file for persistence**` 
	`_echo "net.ipv4.ip_forward = 1" | sudo tee /etc/sysctl.d/99-forwarding.conf_` 
	`**# Apply the setting immediately without rebooting**` 
	`_sudo sysctl -p /etc/sysctl.d/99-forwarding.conf # You can verify with: sysctl net.ipv4.ip_forward_`	

	`**Assign Interfaces and Sources to**` `**firewalld**` `**Zones:**`
	`**# Assign the internet interface to the 'external' zone**` 
	`_sudo firewall-cmd --permanent --zone=external --change-interface=wlp4s0_` 
	`**# Assign the hotspot bridge interface to the '**``**internal**``**' zone**` 
	`_sudo firewall-cmd --permanent --zone=_``_internal_` `_--change-interface=br-hotspot_` 
	`**# (Optional**``**)**` `**Explicitly assign the source subnet to '**``**internal**``**' zone**` 
	`_sudo firewall-cmd --permanent --zone=_``_internal_` `_–add-source=192.168.42.0/24_`

	`**Enable Masquerading (NAT) on the External Zone:**`
	`_sudo firewall-cmd --permanent --zone=external --add-masquerade_` 

	`**Create Policy to Allow Forwarding from**` `**internal**` `**to**` `**external**`
	`**# Create the policy object**` 
	`_sudo firewall-cmd --permanent --new-policy P__``_Internal2_``_External_` 
	`**# Define traffic source zone (ingress)**` 
	`_sudo firewall-cmd --permanent --policy P__``_Internal2_``_External --add-ingress-zone=_``_internal_` 
	`**# Define traffic destination zone (egress)**` 
	`_sudo firewall-cmd --permanent --policy P__``_Internal2_``_External --add-egress-zone=external_` 
	`**# Set the policy action to allow the traffic**` 
	`_sudo firewall-cmd --permanent --policy P__``_Internal2_``_External --set-target ACCEPT_`
	
	`**Apply All firewalld Changes**`
	`_sudo firewall-cmd --reload_`

**Step 4: Configure Libvirt Network**

Now, tell libvirt about the existing host bridge (`br-hotspot`) so VMs can connect to it.

1.  **Create a libvirt network XML file** (e.g., `hotspot-bridged.xml`):
    
    XML
    
    `<network>`
      `<name>hotspot-bridged-net</name> <forward mode='bridge'/>      <bridge name='br-hotspot'/>    </network>`
    
    *   Note: We use `<forward mode='bridge'/>` because the bridge (`br-hotspot`) already exists on the host and handles the Layer 2 connections. Libvirt doesn't need to manage DHCP or IP addressing for this network; `dnsmasq` is doing that on the host bridge.
        
2.  **Define and Start the Libvirt Network:**
    
    Bash
    
    `sudo virsh net-define hotspot-bridged.xml`
    `sudo virsh net-start hotspot-bridged-net`
    `sudo virsh net-autostart hotspot-bridged-net # Make it start automatically with libvirt`
    

**Step 5: Configure the Virtual Machine**

1.  Open `virt-manager`.
    
2.  Select your VM and go to its "Hardware Details".
    
3.  Select the Network Interface Card (NIC).
    
4.  Under "Network source", choose "Virtual network 'hotspot-bridged-net': Bridge host device br-hotspot".
    
5.  Ensure the "Device model" is set to `virtio` for best performance.
    
6.  Apply the changes.
    

**Step 6: Start and Test**

1.  Start your VM.
    
2.  Inside the VM, check its IP address. It should receive an IP from the `192.168.``42``.x` range via DHCP from the `dnsmasq` service running on the host.
    
3.  Connect a client device (phone, another laptop) to the "Your\_5GHz\_Hotspot" Wi-Fi network you created.
    
4.  Check the client device's IP address. It should also be in the `192.168.``42``.x` range.
    
5.  **Test Connectivity:**
    
    *   Ping the VM's IP address from the hotspot client.
        
    *   Ping the hotspot client's IP address from the VM.
        
    *   Try accessing services running on the VM (like Sunshine) from the client using the VM's IP address (e.g., `http://<VM_IP>:<Sunshine_Port>`). No port forwarding should be needed.
        
    *   Verify both the VM and the hotspot client can access the internet (e.g., ping `8.8.8.8` or browse a website).
        

This setup creates a bridged network (`br-hotspot`) where your VM and your hotspot clients reside on the same logical network segment (192.168.42.0/24). The host routes traffic between this network and the internet using its primary Wi-Fi connection (`wlan0`) via NAT. This directly fulfills the requirement for clients to access any service on the VM without manual port forwarding.
* * *
