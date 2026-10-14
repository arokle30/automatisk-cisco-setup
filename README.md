Scriptet er satt opp sånn at det er default valg som vil gi et fungerene produkt, mens du kan også velge andre porter/navn som passer deg bedre.
Hvis du bruker en: catalyst 3650 som core, cisco 4221 som R1 og R2, catalyst 2960 som SW1 og SW2, Kan du bruke alle defaoult valg.

1. SSH oppsett
   Kjør ssh_setup.py mens du er koblet til enheten med consol kabel. Gå igjenom oppsette.

   Etter du har kjørt ssh_setup.py, oppdater hosts.ini filen med riktig ip, brukernavn og passord.

3. Ansible playbooks
   Hvis du har gitt core switchen en ip som 172.16.5.x, må PCen med Ansible ha en statisk ip som: 172.16.5.10 / 255.255.255.0, gateway 172.16.5.1.
   Kjør i denne rekkefølgen. Switchene nås bare etter at R1 er ferdig.
    ansible-playbook core.yml
    ansible-playbook r1.yml
    ansible-playbook r2.yml
    ansible-playbook sw1.yml
    ansible-playbook sw2.yml

4. Sjekk at oppsette fungerte
   ansible-playbook verify.yml

5. Ferdig
   Nå kan du teste om de to PCene på VLAN 10 og 20 kan pinge hverandre/pinge andre enheter.
