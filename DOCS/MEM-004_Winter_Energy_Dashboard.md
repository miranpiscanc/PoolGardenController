# MeM Software Requirements Specification

## Module

**MEM-004 -- Winter Energy Dashboard (Learning Mode)**

**Version:** 1.0 (Draft)

## 1. Executive Summary

This document defines the requirements for the Winter Energy module of
MeM. The purpose of this module is to learn the thermal behaviour of the
Cesclans house and prepare the infrastructure for future predictive HVAC
automation.

## 2. Design Philosophy

The goal is **not** to control HVAC units directly. The goal is to
maximize photovoltaic self-consumption while maintaining indoor comfort
and protecting battery autonomy. HVAC units are actuators. The decision
engine is the Energy Manager.

## 3. Objectives

-   Collect thermal and energy data every 5 minutes.
-   Build a historical dataset.
-   Estimate thermal inertia.
-   Display a dedicated dashboard.
-   Produce simulated decisions only.
-   Never send HVAC commands in this phase.

## 4. Existing Architecture

Reuse existing: - GoodWe service - Netatmo service - Intesis service -
Scheduler - SQLite database - Logging - Flask routes - Templates - CSS /
JavaScript

## 5. Functional Requirements

### 5.1 Learning Mode

-   Automation disabled.
-   Simulation enabled.
-   Historical acquisition active.

### 5.2 Netatmo

Collect: - Outdoor temperature - Master bedroom - Kids room Calculate: -
Indoor average - Coldest room - Indoor/outdoor delta - Temperature
trends

### 5.3 GoodWe

Collect: - PV production - House consumption - Battery SOC - Battery
charge/discharge - Grid import/export

### 5.4 Historical Storage

Create table: `winter_energy_history`

Sampling: - Every 300 seconds

Store NULL when data are unavailable.

### 5.5 Dashboard

Sections: 1. System status 2. Temperature cards 3. Energy cards 4. HVAC
cards 5. Thermal analysis 6. Simulated decision 7. Historical charts

### 5.6 Simulated Decision Engine

Generate recommendations only. Never control HVAC.

Banner: \> SIMULATION ONLY -- NO HVAC COMMANDS ARE SENT.

## 6. REST API

-   GET /api/winter/current
-   GET /api/winter/history
-   GET /api/winter/status

## 7. Configuration

-   WINTER_ENABLED
-   WINTER_MODE
-   WINTER_TARGET_TEMP
-   WINTER_AUTOMATION
-   WINTER_SIMULATION

## 8. Reliability

-   Thread-safe
-   No duplicated collectors
-   Graceful degradation
-   Partial sample persistence

## 9. UI

Responsive. Desktop + iPhone. Viewer is read-only.

## 10. Acceptance Criteria

-   Dashboard operational
-   Data collected every 5 minutes
-   Charts available
-   Simulation active
-   Zero HVAC commands

## 11. Roadmap

MEM-005: HVAC Automation MEM-006: Energy Manager Core MEM-007:
Predictive Thermal Model

------------------------------------------------------------------------

This document is the baseline specification and will evolve together
with the MeM project.
