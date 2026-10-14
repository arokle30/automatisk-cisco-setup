"""
cisco_serial.py - Felles kode for å konfigurere Cisco-enheter over konsollkabel.

Brukes av ssh_setup.py. Inneholder:
  * CiscoConsole  - en klasse som snakker med enheten via pyserial
  * input-hjelpere - spør brukeren om verdier og validerer dem
  * felles SSH-kommandoer og selve kjøringen (apply_config)
"""

import argparse
import getpass
import ipaddress
import platform
import re
import sys
import time

try:
    import serial
    from serial.tools import list_ports
except ImportError:
    sys.exit("pyserial er ikke installert. Kjør:  pip install -r requirements.txt")


# Matcher en Cisco-prompt på slutten av bufferet, f.eks. "Switch>", "R1#", "SW1(config-if)#"
PROMPT_RE = re.compile(r"[\w.\-()]+[>#]\s*$")
# Ting vi kan møte når vi "vekker" konsollen
WAKE_RE = re.compile(
    r"([\w.\-()]+[>#]\s*$|\[yes/no\]:?\s*$|\[yes\]:\s*$|Username:\s*$|Password:\s*$|RETURN to get started)",
    re.IGNORECASE,
)
# Feilmeldinger fra IOS
ERROR_RE = re.compile(r"^\s*%\s*(Invalid|Incomplete|Ambiguous|Unknown|Bad|Please)", re.MULTILINE)


class CommandError(RuntimeError):
    """En kommando ga feil på enheten. Scriptet stopper før noe lagres."""

    def __init__(self, command, output):
        lines = [l.strip() for l in output.splitlines() if l.strip().startswith("%")]
        self.command = command
        self.message = lines[0] if lines else output.strip()[-120:]
        super().__init__(f"'{command}' feilet: {self.message}")


# --------------------------------------------------------------------------- #
#  Seriell kommunikasjon
# --------------------------------------------------------------------------- #
class CiscoConsole:
    """Enkel wrapper rundt pyserial for Cisco IOS-konsoll (9600 8N1, ingen flytkontroll)."""

    def __init__(self, port, baudrate=9600, verbose=True):
        self.verbose = verbose
        self.errors = []
        self.ser = serial.Serial(
            port=port,
            baudrate=baudrate,
            bytesize=serial.EIGHTBITS,
            parity=serial.PARITY_NONE,
            stopbits=serial.STOPBITS_ONE,
            xonxoff=False,
            rtscts=False,
            timeout=0.2,  # kort timeout slik at read() ikke blokkerer lenge
        )

    # Gjør det mulig å bruke "with CiscoConsole(...) as dev:"
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def close(self):
        if self.ser.is_open:
            self.ser.close()

    def write(self, text, char_delay=0.003):
        """Skriv tegn for tegn. Travle enheter (f.eks. ISR 4000 under oppstart) mister
        ellers tegn når alt kommer på én gang."""
        for ch in text.encode("ascii", errors="ignore"):
            self.ser.write(bytes([ch]))
            self.ser.flush()
            time.sleep(char_delay)

    def drain(self):
        """Kast det som allerede ligger i mottaksbufferet (gamle prompter, loggmeldinger)."""
        time.sleep(0.1)
        self.ser.reset_input_buffer()

    def wait_quiet(self, quiet=3, max_wait=300):
        """Vent til enheten har vært stille i 'quiet' sekunder (f.eks. etter oppstart)."""
        print("\n[info] Venter til enheten er ferdig med å skrive meldinger ...", flush=True)
        last = time.time()
        deadline = time.time() + max_wait
        while time.time() < deadline and time.time() - last < quiet:
            data = self.ser.read(self.ser.in_waiting or 1)
            if data:
                last = time.time()
                if self.verbose:
                    print(data.decode("ascii", errors="ignore"), end="", flush=True)

    def read_until(self, pattern=PROMPT_RE, timeout=15):
        """Les fra porten til 'pattern' matcher eller tiden går ut."""
        if isinstance(pattern, str):
            pattern = re.compile(pattern)
        buf = ""
        deadline = time.time() + timeout
        while time.time() < deadline:
            data = self.ser.read(self.ser.in_waiting or 1)
            if not data:
                continue
            text = data.decode("ascii", errors="ignore")
            buf += text
            if self.verbose:
                print(text, end="", flush=True)
            if "--More--" in buf:  # side-for-side-visning: trykk mellomrom
                buf = buf.replace("--More--", "")
                self.write(" ")
            if pattern.search(buf):
                return buf
        raise TimeoutError(f"Ingen forventet respons innen {timeout}s. Siste mottatt: {buf[-200:]!r}")

    @staticmethod
    def last_line(text):
        lines = text.strip().splitlines()
        return lines[-1].strip() if lines else ""

    def send(self, command, expect=PROMPT_RE, timeout=15, hide=False, allow_error=False):
        """Send én kommando og vent på prompt. hide=True skjuler passord i utskriften."""
        old_verbose = self.verbose
        if hide:
            print(f"\n[sender] {command.rsplit(' ', 1)[0]} *****", flush=True)
            self.verbose = False
        try:
            self.drain()  # ellers kan en gammel prompt bli tatt som svar på denne kommandoen
            self.write(command + "\r")
            out = self.read_until(expect, timeout)
        finally:
            self.verbose = old_verbose
        if ERROR_RE.search(out):
            shown = command.rsplit(" ", 1)[0] + " *****" if hide else command
            if not allow_error:
                raise CommandError(shown, out)
            print(f"\n[info] Enheten godtok ikke: {shown} (ikke kritisk, fortsetter)")
            return None
        return out

    def wake_up(self, attempts=10):
        """Får kontakt med enheten og håndterer oppstartsdialog og konsoll-innlogging."""
        send_cr = True
        for _ in range(attempts):
            if send_cr:
                self.write("\r")
            send_cr = True
            try:
                out = self.read_until(WAKE_RE, timeout=6)
            except TimeoutError:
                continue
            tail = out.rstrip()[-100:]
            if "[yes/no]" in tail:  # "Would you like to enter the initial configuration dialog?"
                self.write("no\r")
                send_cr = False
            elif tail.endswith("Username:"):
                self.write(input("\nKonsollen ber om brukernavn: ") + "\r")
                send_cr = False
            elif tail.endswith("Password:"):
                self.write(getpass.getpass("\nKonsollen ber om passord: ") + "\r")
                send_cr = False
            elif PROMPT_RE.search(out):
                return self.last_line(out)
            # "Press RETURN to get started" / "[yes]:" -> neste runde sender Enter
        raise RuntimeError("Fikk ikke kontakt med enheten. Sjekk kabel, port og baudrate.")

    def enter_enable(self):
        """Går til privileged EXEC-modus (#)."""
        prompt = self.wake_up()
        self.wait_quiet()
        self.drain()
        self.write("\r")
        prompt = self.last_line(self.read_until(WAKE_RE, timeout=10))
        if "(config" in prompt:
            prompt = self.last_line(self.send("end"))
        if prompt.endswith(">"):
            self.write("enable\r")
            out = self.read_until(r"(Password:\s*$|[>#]\s*$)")
            if out.rstrip().endswith("Password:"):
                pw = getpass.getpass("\nEnheten har allerede et enable-passord. Skriv det inn: ")
                self.write(pw + "\r")
                out = self.read_until(r"(Password:\s*$|[>#]\s*$)")
            if not out.rstrip().endswith("#"):
                raise RuntimeError("Kom ikke inn i enable-modus (feil passord?).")

    def generate_rsa_key(self, bits=2048):
        """Genererer RSA-nøkler. Svarer 'yes' hvis det allerede finnes nøkler."""
        self.drain()
        self.write(f"crypto key generate rsa general-keys modulus {bits}\r")
        out = self.read_until(r"(\[yes/no\]:\s*$|[>#]\s*$)", timeout=180)
        if "[yes/no]" in out:
            self.write("yes\r")
            out = self.read_until(r"[>#]\s*$", timeout=180)
        if ERROR_RE.search(out):
            raise CommandError("crypto key generate rsa", out)
        return out


# --------------------------------------------------------------------------- #
#  Input fra brukeren
# --------------------------------------------------------------------------- #
def ask(prompt, default=None, validator=None, required=True):
    """Spør brukeren om en verdi. Tom input gir default."""
    while True:
        suffix = f" [{default}]" if default else ""
        value = input(f"{prompt}{suffix}: ").strip() or (default or "")
        if not value:
            if not required:
                return ""
            print("  Verdien kan ikke være tom.")
            continue
        if validator:
            try:
                validator(value)
            except ValueError as err:
                print(f"  Ugyldig verdi: {err}")
                continue
        return value


def ask_password(prompt, min_len=4):
    """Spør om passord to ganger uten å vise det på skjermen."""
    while True:
        pw = getpass.getpass(f"{prompt}: ")
        if len(pw) < min_len:
            print(f"  Passordet må være minst {min_len} tegn.")
        elif pw.startswith(" ") or "?" in pw:
            print("  Passordet kan ikke starte med mellomrom eller inneholde '?' (IOS tolker det som hjelp).")
        elif pw != getpass.getpass("Bekreft passord: "):
            print("  Passordene er ikke like, prøv igjen.")
        else:
            return pw


def validate_hostname(value):
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9\-]{0,62}", value):
        raise ValueError("må starte med bokstav og kun inneholde bokstaver, tall og bindestrek")
    if value.lower() in ("switch", "router"):
        raise ValueError("RSA-nøkler krever et annet hostname enn standardnavnet")


def validate_ip(value):
    try:
        ipaddress.IPv4Address(value)
    except ipaddress.AddressValueError:
        raise ValueError("ikke en gyldig IPv4-adresse")


def validate_mask(value):
    try:
        ok = ipaddress.IPv4Network(f"0.0.0.0/{value}").netmask == ipaddress.IPv4Address(value)
    except ValueError:
        ok = False
    if not ok:
        raise ValueError("ikke en gyldig subnettmaske (f.eks. 255.255.255.0)")


def validate_range(low, high):
    def _check(value):
        if not value.isdigit() or not low <= int(value) <= high:
            raise ValueError(f"må være et tall mellom {low} og {high}")
    return _check


def validate_interface(value):
    if not re.fullmatch(r"[A-Za-z\-]+\d+(/\d+)*(\.\d+)?", value):
        raise ValueError("f.eks. GigabitEthernet0/0 eller g0/1")


# --------------------------------------------------------------------------- #
#  Argumenter og porter
# --------------------------------------------------------------------------- #
def default_port():
    return "COM3" if platform.system() == "Windows" else "/dev/ttyUSB0"


def build_arg_parser(description):
    p = argparse.ArgumentParser(description=description)
    p.add_argument("-p", "--port", help=f"seriell port, f.eks. COM3, /dev/ttyS3 eller /dev/ttyUSB0 (standard spør)")
    p.add_argument("-b", "--baud", type=int, default=9600, help="baudrate (standard 9600)")
    p.add_argument("--list-ports", action="store_true", help="vis tilgjengelige serielle porter og avslutt")
    p.add_argument("--dry-run", action="store_true", help="vis kommandoene uten å koble til enheten")
    p.add_argument("-q", "--quiet", action="store_true", help="ikke vis output fra enheten")
    return p


def print_ports():
    ports = list(list_ports.comports())
    if not ports:
        print("Fant ingen serielle porter.")
    for p in ports:
        print(f"{p.device:20} {p.description}")


def choose_port(args):
    if args.port:
        return args.port
    ports = list(list_ports.comports())
    if not ports:
        print("Fant ingen porter automatisk.")
        return ask("Seriell port", default=default_port())
    print("\nTilgjengelige serielle porter:")
    for i, p in enumerate(ports, 1):
        print(f"  {i}) {p.device} - {p.description}")
    choice = ask("Velg port (nummer eller navn)", default="1")
    if choice.isdigit() and 1 <= int(choice) <= len(ports):
        return ports[int(choice) - 1].device
    return choice


# --------------------------------------------------------------------------- #
#  Felles SSH-konfigurasjon
# --------------------------------------------------------------------------- #
def ask_common_settings(default_hostname):
    print("\n=== Grunnleggende innstillinger ===")
    return {
        "hostname": ask("Hostname", default=default_hostname, validator=validate_hostname),
        "domain": ask("Domenenavn (kreves for RSA-nøkler)", default="lab.local"),
        "username": ask("Brukernavn for SSH", default="admin"),
        "password": ask_password("Passord for SSH-brukeren"),
        "enable_secret": ask_password("Enable secret"),
        "rsa_bits": int(ask("RSA-nøkkelstørrelse", default="2048", validator=validate_range(1024, 4096))),
    }


def base_commands(cfg):
    """Kommandoer som må kjøres FØR RSA-nøklene genereres."""
    return [
        f"hostname {cfg['hostname']}",
        "no ip domain-lookup",
        f"ip domain-name {cfg['domain']}",
        f"enable secret {cfg['enable_secret']}",
        f"username {cfg['username']} privilege 15 secret {cfg['password']}",
        "service password-encryption",
    ]


def ssh_commands(vty_range):
    """Kommandoer som kjøres ETTER at RSA-nøklene finnes."""
    return [
        "ip ssh version 2",
        "ip ssh time-out 60",
        "ip ssh authentication-retries 3",
        f"line vty {vty_range}",
        "login local",
        "transport input ssh",
        "exec-timeout 10 0",
        "exit",
    ]


# Kommandoer som ikke finnes på alle modeller - feil her stopper ikke scriptet
OPTIONAL_CMDS = {"no ip domain-lookup", "service password-encryption"}
OPTIONAL_PREFIXES = ("ip ssh time-out", "ip ssh authentication-retries", "exec-timeout")


def apply_config(args, cfg, pre_key_cmds, post_key_cmds, verify_cmds):
    """Kobler til enheten og sender hele konfigurasjonen."""
    secrets = [cfg["password"], cfg["enable_secret"]]

    def is_secret(cmd):
        return any(s in cmd for s in secrets)

    if args.dry_run:
        all_cmds = ["enable", "terminal length 0", "configure terminal", "no logging console", *pre_key_cmds,
                    f"crypto key generate rsa general-keys modulus {cfg['rsa_bits']}",
                    *post_key_cmds, "logging console", "end", "write memory", *verify_cmds]
        print("\n--- Tørrkjøring: disse kommandoene ville blitt sendt ---")
        for c in all_cmds:
            print(c.rsplit(" ", 1)[0] + " *****" if is_secret(c) else c)
        return

    port = choose_port(args)
    print(f"\nKobler til {port} ({args.baud} baud) ...")
    try:
        dev = CiscoConsole(port, args.baud, verbose=not args.quiet)
    except serial.SerialException as err:
        sys.exit(f"Kunne ikke åpne {port}: {err}\n"
                 "Tips: lukk PuTTY/screen, sjekk portnavnet og (Linux) at du er i gruppen 'dialout'.")

    try:
        with dev:
            dev.enter_enable()
            dev.send("terminal length 0", allow_error=True)
            dev.send("configure terminal")
            # Loggmeldinger på konsollen ødelegger dialogen - slå av mens vi jobber
            dev.send("no logging console", allow_error=True)
            for c in pre_key_cmds:
                if c.startswith("ip domain-name "):
                    # Eldre IOS: "ip domain-name", nyere IOS-XE: "ip domain name"
                    if dev.send(c, allow_error=True) is None:
                        dev.send(c.replace("ip domain-name ", "ip domain name "))
                    continue
                dev.send(c, hide=is_secret(c), allow_error=c in OPTIONAL_CMDS)
            print("\n[info] Genererer RSA-nøkler, dette kan ta opptil et par minutter ...")
            dev.generate_rsa_key(cfg["rsa_bits"])
            for c in post_key_cmds:
                dev.send(c, allow_error=c.startswith(OPTIONAL_PREFIXES))
            dev.send("logging console", allow_error=True)
            dev.send("end")
            dev.send("write memory", timeout=60)
            for c in verify_cmds:
                dev.send(c, timeout=30, allow_error=True)
    except CommandError as err:
        sys.exit(f"\n[FEIL] {err}\n"
                 "Scriptet stoppet før 'write memory', så ingenting er lagret.\n"
                 "Sjekk kommandoen/porten, og kjør scriptet på nytt (det er trygt å kjøre flere ganger).")
    except (TimeoutError, RuntimeError) as err:
        sys.exit(f"\n[FEIL] {err}")
    except KeyboardInterrupt:
        sys.exit("\nAvbrutt av bruker.")

    print("\n" + "=" * 60)
    print("Ferdig! SSH er konfigurert og lagret.")
    print("=" * 60)
