# ☀
# 💧 ⚡ 🌡
# PoolGardenController
Designed and continuously developed on a real residential pool installation in Italy.

### Smart Pool & Garden Automation

A professional web-based automation controller for swimming pools and garden equipment.

PoolGardenController is an open-source Python application designed to automate, monitor and safely manage swimming pool equipment using standard Ethernet devices.

Originally developed for a real residential pool installation, it has evolved into a complete automation platform focused on **reliability, safety and simplicity**.

---

## ✨ Features

### 🏊 Pool Management

- Manual circulation pump control
- Manual electric heater control
- Automatic daily scheduler
- Manual override
- Automatic Solar Heating Automation
- Immediate heater shutdown

---

### 🌡 Temperature Monitoring

- HWg-STE Ethernet thermometer support
- Water temperature monitoring
- Outdoor temperature monitoring
- Automatic Online / Offline detection
- Live dashboard updates

---

### 🔥 Safety First

PoolGardenController never replaces the safety devices built into the equipment.

Instead it coordinates the operation of the system while leaving thermal and electrical protections to the original hardware.

Current safety features include:

- Immediate Relay 1 shutdown
- Continued circulation through the heater after shutdown
- Browser-independent operation
- Automatic recovery after browser refresh

---

### 📊 Monitoring & Statistics

- Live Dashboard
- Device runtime statistics
- Heater operating hours
- Daily statistics
- Seasonal statistics
- Event Log
- Software version display

---

### ⚙ Configuration

- JSON configuration
- Persistent runtime data
- Automatic recovery of missing files
- Configurable scheduler
- Configurable Solar Heating Automation

---

## ☀ Solar Heating Automation

One of the key features of PoolGardenController.

Instead of acting as a thermostat, the controller intelligently decides **when the heater is allowed to operate**, taking advantage of daytime solar production.

The heater's own thermostat remains responsible for regulating the water temperature.

Current configurable parameters include:

- Water temperature threshold
- Automatic start time
- Automatic stop time

Future versions will also consider photovoltaic production and household power consumption to optimize energy usage.

---

## 🖥 Dashboard

The web interface provides real-time information including:

- Pump status
- Heater status
- Garden lighting
- Water temperature
- Outdoor temperature
- Solar Heating Automation
- Statistics
- Event Log
- Device communication status

Everything can be managed from a clean and responsive interface.

---

## 🔧 Supported Hardware

Currently supported:

- Ethernet Relay Controller
- HWg-STE Ethernet Thermometers

Planned support:

- GoodWe GW6000-ES inverter
- Modbus TCP devices
- Additional Ethernet sensors

---

## 💻 Technologies

- Python
- Flask
- HTML5
- JavaScript
- CSS
- JSON
- Git
- GitHub

---

## 📸 Screenshots

### Dashboard

*(Screenshot coming soon)*

### Statistics

*(Screenshot coming soon)*

### Event Log

*(Screenshot coming soon)*

---

## 🚀 Installation

Clone the repository:

```bash
git clone https://github.com/miranpiscanc/PoolGardenController.git
cd PoolGardenController
```

Install dependencies:

```bash
pip install -r requirements.txt
```

Start the application:

```bash
python app.py
```

Open your browser:

```
http://localhost:5000
```

---

## 🛣 Roadmap

Planned future developments:

- ☀ GoodWe inverter integration
- ⚡ Energy-aware automation
- 🌤 Weather forecast integration
- 📈 Solar production monitoring
- 🔋 Smart energy management
- 🌍 Multi-language interface
- 📦 Backup & Restore
- 📱 Mobile UI improvements
- 🔌 MQTT support

---

## 📖 Project Philosophy

PoolGardenController has been designed around a simple principle:

> **Automation should simplify life without compromising safety.**

Every new feature is developed for a real installation and tested under real operating conditions before becoming part of the project.

The goal is to create a reliable, maintainable and easy-to-use automation platform rather than simply controlling relays.

---

## 📈 Project Status

🟢 **Active Development**

New features are continuously added and tested on a real residential installation.

---

## 👤 Author

**Miran Piščanc**

Trieste, Italy 🇮🇹

---

## 📄 License

MIT License

---

## ⭐ Support the Project

If you find this project useful, consider giving it a ⭐ on GitHub.

Suggestions, improvements and ideas are always welcome.
