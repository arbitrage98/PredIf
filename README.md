AI-Powered DC Motor Predictive Maintenance System
A full-stack Industrial IoT (IIoT) solution for real-time monitoring and predictive health analysis of DC motors. This system integrates physical sensor data with a machine learning-ready backend and a cloud-synced dashboard.

🚀 Overview
This project monitors critical motor parameters (Temperature and Vibration) to prevent mechanical failure. It utilizes a "Hybrid" data approach:

Local Control: High-speed Serial communication for autonomous safety commands.

Cloud Sync: Consentium IoT integration for remote monitoring.

AI Intelligence: Predictive analysis using GPT-5 to assess failure risks.

🏗️ Architecture
Hardware: ESP32 Microcontroller, DHT22 (Temp), ADXL345 (Vibration).

Backend: FastAPI (Python), Motor (Async MongoDB Driver), PySerial.

Frontend: React.js, Tailwind CSS/Custom CSS (Responsive Dashboard).

Database: MongoDB (Time-series sensor logs & user auth).

Cloud: Consentium IoT Platform.

AI: Emergent LLM (GPT-5) for health status prediction.

🛠️ Features
Real-Time Monitoring: Live data streaming from ESP32 to a web-based dashboard.

Closed-Loop Control: Backend automatically sends emergency stop/cool commands to the ESP32 via Serial when thresholds are exceeded.

AI Diagnostics: Predictive maintenance reports providing health status (Good/Fair/Poor) and failure risk percentages.

Data Redundancy: Dual-stream data flow to both local MongoDB and Consentium Cloud.

Secure Auth: JWT-based authentication with protected API routes.
