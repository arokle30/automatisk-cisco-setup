#!/usr/bin/env python3
"""
ssh_setup.py - STUD1: Setter opp SSH via konsollkabel, slik at Ansible kan ta over.

    MGMT_STUD1 -- Gi1/0/3 CORE (3650) Gi1/0/1 -- Gi0/0/1 R1 (ISR4321) Gi0/0/0 -- Fa0/3 SW1
                                      Gi1/0/2 -- Gi0/0/1 R2 (ISR4321) Gi0/0/0 -- Fa0/4 SW1
    SW1 (2960) Fa0/2 -- Fa0/3 SW2 (2960)
    SW2 Fa0/1 = VLAN10-PC, Fa0/2 = VLAN20-PC, Fa0/4-5 = EtherChannel mot STUD2-SW2 Fa0/4-5

Velg enhet (CORE, R1, R2, SW1, SW2). Alle navn, porter og adresser kan endres
når scriptet spør. Trykk Enter for å bruke standardverdien i [klammer].

Eksempel:
    python3 ssh_setup.py                          # spør om enhet og port
    python3 ssh_setup.py -d R1 -p /dev/ttyS1      # COM1 i WSL
    python3 ssh_setup.py -d SW2 --dry-run         # vis kommandoene uten å sende dem
"""

import re

from cisco_serial import (apply_config, ask, ask_common_settings, base_commands,
                          build_arg_parser, print_ports, ssh_commands,
                          validate_interface, validate_ip, validate_mask, validate_range)

# --------------------------------------------------------------------------- #
#  Standardverdier for STUD1 (fra diagrammet). Endre her, eller svar noe
#  annet når scriptet spør.
# --------------------------------------------------------------------------- #
DEVICES = {'CORE': {'label': 'CORE - Catalyst 3650 (multilayer)',
          'kind': 'l3switch',
          'hostname': 'S1-CORE',
          'vty': '0 15',
          'interface': 'GigabitEthernet1/0/3',
          'description': 'MGMT_STUD1',
          'ip': '172.16.5.1',
          'mask': '255.255.255.0'},
 'R1': {'label': 'R1 - ISR 4321',
        'kind': 'router',
        'hostname': 'S1-R1',
        'vty': '0 4',
        'interface': 'GigabitEthernet0/0/1',
        'description': 'Link til CORE',
        'ip': '172.16.3.2',
        'mask': '255.255.255.0',
        'route_net': '172.16.5.0',
        'route_mask': '255.255.255.0',
        'next_hop': '172.16.3.1'},
 'R2': {'label': 'R2 - ISR 4321',
        'kind': 'router',
        'hostname': 'S1-R2',
        'vty': '0 4',
        'interface': 'GigabitEthernet0/0/1',
        'description': 'Link til CORE',
        'ip': '172.16.4.2',
        'mask': '255.255.255.0',
        'route_net': '172.16.5.0',
        'route_mask': '255.255.255.0',
        'next_hop': '172.16.4.1'},
 'SW1': {'label': 'SW1 - Catalyst 2960 (Switch3)',
         'kind': 'switch',
         'hostname': 'S1-SW1',
         'vty': '0 15',
         'vlans': '10,20',
         'mgmt_vlan': '10',
         'ip': '192.168.10.4',
         'mask': '255.255.255.0',
         'gateway': '192.168.10.1',
         'trunk_ports': 'Fa0/2-4',
         'allowed': '10,20'},
 'SW2': {'label': 'SW2 - Catalyst 2960 (Switch0)',
         'kind': 'switch',
         'hostname': 'S1-SW2',
         'vty': '0 15',
         'vlans': '10,20',
         'mgmt_vlan': '10',
         'ip': '192.168.10.5',
         'mask': '255.255.255.0',
         'gateway': '192.168.10.1',
         'trunk_ports': 'Fa0/3',
         'allowed': '10,20',
         'ec_ports': 'Fa0/4-5',
         'ec_group': '1',
         'ec_mode': 'active'}}

ETHERCHANNEL_MODES = ("active", "passive", "on", "desirable", "auto")

# Ett eller flere interface, med område og komma: "Fa0/1-2, Fa0/4"
PORT_RE = re.compile(r"[A-Za-z]+\s*\d+(/\d+)*(\s*-\s*\d+)?")
VLAN_LIST_RE = re.compile(r"\d+(\s*,\s*\d+)*")


# --------------------------------------------------------------------------- #
#  Validering og hjelpere
# --------------------------------------------------------------------------- #
def validate_ports(value):
    if value == "-":
        return
    if not all(PORT_RE.fullmatch(part.strip()) for part in value.split(",")):
        raise ValueError("bruk f.eks. Fa0/1, Fa0/4-5 eller Fa0/1-2, Fa0/4 (skriv '-' for ingen)")


def validate_vlan_list(value):
    if not VLAN_LIST_RE.fullmatch(value):
        raise ValueError("bruk f.eks. 10 eller 10,20")
    for v in value.split(","):
        validate_range(1, 1001)(v.strip())


def validate_vlan_name(value):
    if not re.fullmatch(r"[A-Za-z0-9_\-]{1,32}", value):
        raise ValueError("maks 32 tegn: bokstaver, tall, _ og -")


def validate_ec_mode(value):
    if value not in ETHERCHANNEL_MODES:
        raise ValueError("velg en av: " + ", ".join(ETHERCHANNEL_MODES))


def range_syntax(ports):
    """'Fa0/1-2, Fa0/4' -> 'Fa0/1 - 2 , Fa0/4' (formatet IOS vil ha)."""
    parts = [re.sub(r"\s*-\s*", " - ", p.strip()) for p in ports.split(",")]
    return " , ".join(parts)


def split_vlans(value):
    return [v.strip() for v in value.split(",") if v.strip()]


def optional(prompt, default, validator):
    """Spør om en verdi som kan droppes med '-'."""
    value = ask(f"{prompt} ('-' = hopp over)", default=default or "-",
                validator=lambda v: None if v == "-" else validator(v))
    return "" if value == "-" else value


# --------------------------------------------------------------------------- #
#  Spørsmål og kommandoer per enhetstype
# --------------------------------------------------------------------------- #
def l3switch_settings(dev):
    """CORE: ip routing + routed port mot MGMT-PC-en."""
    print("\n=== Port mot MGMT-PC (routed port) ===")
    interface = ask("Interface", default=dev["interface"], validator=validate_interface)
    description = ask("Beskrivelse", default=dev["description"], required=False)
    ip = ask(f"IP-adresse på {interface}", default=dev["ip"], validator=validate_ip)
    mask = ask("Subnettmaske", default=dev["mask"], validator=validate_mask)

    cmds = ["ip routing", f"interface {interface}"]
    if description:
        cmds.append(f"description {description}")
    cmds += ["no switchport", f"ip address {ip} {mask}", "no shutdown", "exit"]
    print(f"\n[info] Gi MGMT-PC-en en IP i samme nett, med gateway {ip}.")
    return cmds, ip, ["show ip ssh", "show ip interface brief"]


def router_settings(dev):
    """R1/R2: IP på linken mot CORE + rute tilbake til MGMT-nettet."""
    print("\n=== Interface mot CORE (SSH nås via dette) ===")
    interface = ask("Interface", default=dev["interface"], validator=validate_interface)
    description = ask("Beskrivelse", default=dev["description"], required=False)
    ip = ask(f"IP-adresse på {interface}", default=dev["ip"], validator=validate_ip)
    mask = ask("Subnettmaske", default=dev["mask"], validator=validate_mask)

    print("\n=== Rute tilbake til MGMT-PC-en (til OSPF er oppe) ===")
    net = optional("MGMT-nettet", dev.get("route_net"), validate_ip)
    route = []
    if net:
        rmask = ask("Maske", default=dev["route_mask"], validator=validate_mask)
        hop = ask("Neste hopp (CORE)", default=dev["next_hop"], validator=validate_ip)
        route = [f"ip route {net} {rmask} {hop}"]

    cmds = [f"interface {interface}"]
    if description:
        cmds.append(f"description {description}")
    cmds += [f"ip address {ip} {mask}", "no shutdown", "exit"] + route
    return cmds, ip, ["show ip ssh", "show ip interface brief", "show ip route static"]


def switch_settings(dev):
    """SW1/SW2: VLAN-er, trunker, EtherChannel og management-IP på en SVI."""
    print("\n=== VLAN-er ===")
    vlans = split_vlans(ask("VLAN-er som skal opprettes", default=dev["vlans"],
                            validator=validate_vlan_list))
    names = {v: ask(f"Navn på VLAN {v}", default=f"VLAN{v}", validator=validate_vlan_name)
             for v in vlans}

    print("\n=== Management-IP (SVI) ===")
    mgmt = ask("Management-VLAN", default=dev["mgmt_vlan"], validator=validate_range(1, 1001))
    ip = ask(f"IP-adresse på interface Vlan{mgmt}", default=dev["ip"], validator=validate_ip)
    mask = ask("Subnettmaske", default=dev["mask"], validator=validate_mask)
    gateway = optional("Default gateway", dev.get("gateway"), validate_ip)

    print("\n=== Trunk-porter (mot ruter / annen switch) ===")
    trunk = optional("Trunk-porter (f.eks. Fa0/1-2, Fa0/4)", dev.get("trunk_ports"), validate_ports)
    allowed = ask("Tillatte VLAN-er på trunker", default=dev["allowed"], validator=validate_vlan_list)
    allowed = allowed.replace(" ", "")

    print("\n=== EtherChannel mot den andre studentens switch ===")
    print("  Begge sider må ha samme porter-antall, samme VLAN-er og en modus som passer:")
    print("  active+active / active+passive (LACP), desirable+auto (PAgP) eller on+on.")
    ec_ports = optional("EtherChannel-porter (f.eks. Fa0/4-5)", dev.get("ec_ports"), validate_ports)
    ec_group = ec_mode = ""
    if ec_ports:
        ec_group = ask("Port-channel-nummer", default=dev.get("ec_group", "1"), validator=validate_range(1, 48))
        ec_mode = ask("Modus", default=dev.get("ec_mode", "active"), validator=validate_ec_mode)

    cmds = []
    for v in vlans:
        cmds += [f"vlan {v}", f"name {names[v]}", "exit"]
    if mgmt not in vlans:
        cmds += [f"vlan {mgmt}", "exit"]
    if trunk:
        cmds += [f"interface range {range_syntax(trunk)}", "switchport mode trunk",
                 f"switchport trunk allowed vlan {allowed}", "no shutdown", "exit"]
    if ec_ports:
        cmds += [f"interface range {range_syntax(ec_ports)}", "switchport mode trunk",
                 f"switchport trunk allowed vlan {allowed}",
                 f"channel-group {ec_group} mode {ec_mode}", "no shutdown", "exit",
                 f"interface port-channel {ec_group}", "switchport mode trunk",
                 f"switchport trunk allowed vlan {allowed}", "exit"]
    cmds += [f"interface vlan {mgmt}", "description Management",
             f"ip address {ip} {mask}", "no shutdown", "exit"]
    if gateway:
        cmds.append(f"ip default-gateway {gateway}")

    verify = ["show ip ssh", "show vlan brief", "show ip interface brief", "show interfaces trunk"]
    if ec_ports:
        verify.append("show etherchannel summary")
    return cmds, ip, verify


SETTINGS = {"l3switch": l3switch_settings, "router": router_settings, "switch": switch_settings}


# --------------------------------------------------------------------------- #
def choose_device(args):
    if args.device:
        return args.device.upper()
    keys = list(DEVICES)
    print("\nHvilken enhet vil du sette opp SSH på?")
    for i, key in enumerate(keys, 1):
        print(f"  {i}) {DEVICES[key]['label']}")
    while True:
        choice = input(f"Velg [1-{len(keys)}]: ").strip()
        if choice.isdigit() and 1 <= int(choice) <= len(keys):
            return keys[int(choice) - 1]
        if choice.upper() in DEVICES:
            return choice.upper()
        print("  Ugyldig valg.")


def main():
    parser = build_arg_parser("STUD1: sett opp SSH på CORE, R1, R2, SW1 eller SW2 via konsollkabel.")
    parser.add_argument("-d", "--device", type=str.upper, choices=list(DEVICES),
                        help="enhet: CORE, R1, R2, SW1 eller SW2 (standard: spør)")
    args = parser.parse_args()

    if args.list_ports:
        print_ports()
        return

    dev = DEVICES[choose_device(args)]
    print(f"\n>>> STUD1: setter opp SSH på {dev['label']}")
    print("    Trykk Enter for standardverdien i [klammer].")
    print("    NB: brukernavn/passord må være de samme som i ../hosts.ini for Ansible.")

    cfg = ask_common_settings(default_hostname=dev["hostname"])
    device_cmds, ip, verify = SETTINGS[dev["kind"]](dev)

    apply_config(
        args, cfg,
        pre_key_cmds=base_commands(cfg) + device_cmds,
        post_key_cmds=ssh_commands(dev["vty"]),
        verify_cmds=verify,
    )
    print(f"Test fra MGMT-PC-en:  ssh {cfg['username']}@{ip}")


if __name__ == "__main__":
    main()
