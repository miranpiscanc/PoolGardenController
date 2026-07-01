Pool & Garden Controller 0.1.0-beta
====================================

Prima versione testabile per HHC-NET2D.

Configurazione già inserita:
- Piscina: 192.168.200.113 porta 5002
  - relè 1 = Riscaldatore
  - relè 2 = Pompa piscina
- Giardino: 192.168.200.117 porta 5002
  - relè 1 = Luci giardino

Avvio:
1) Se Flask non è installato, eseguire INSTALLA_DIPENDENZE.bat
2) Eseguire START_POOL_CONTROLLER.bat
3) Aprire da browser: http://192.168.10.135:5000

Note importanti:
- Questa è una beta: provala prima con attenzione.
- Lo stato viene letto con read1/read2.
- I comandi usati sono quelli visti nella cattura: on1:00, on2:00, off1, off2.
- I log giornalieri sono nella cartella logs.
- La configurazione runtime è nel file data/config.json; i valori di fabbrica sono in config.defaults.json.

Logica pompa:
- In automatico parte alle 09:00 per 8 ore.
- Se accendi manualmente, parte un timer manuale della durata impostata.
- Se spegni manualmente, l'automatico viene sospeso fino al giorno dopo.
- Se la VM si riavvia dentro la fascia automatica, prova a riallineare la pompa allo stato corretto.
