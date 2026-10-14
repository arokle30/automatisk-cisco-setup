1. SSH oppsett
   Kjør ssh_setup.py mens du er koblet til enheten med consol kabel. Gå igjenom oppsette.

2. Ansible playbooks
   Kjør i denne rekkefølgen. Switchene nås bare etter at R1 er ferdig.
    ansible-playbook core.yml
    ansible-playbook r1.yml
    ansible-playbook r2.yml
    ansible-playbook sw1.yml
    ansible-playbook sw2.yml

3. Sjekk at oppsette fungerte
   ansible-playbook verify.yml

4. Ferdig
   Nå kan du teste om de to PCene på VLAN 10 og 20 kan pinge hverandre/pinge andre enheter.
