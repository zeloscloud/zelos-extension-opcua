"""Simulated PLC device for OPC-UA demo mode.

Uses asyncua to run a local OPC-UA server with realistic
PLC data that changes over time.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import math
import random
import threading
import time

from asyncua import Server, ua

logger = logging.getLogger(__name__)

# Demo namespace
DEMO_NAMESPACE = "urn:zelos:demo:plc"


class PLCSimulator:
    """Simulates a PLC with various sensors and actuators."""

    def __init__(self) -> None:
        """Initialize simulator state."""
        self.start_time = time.time()

        # Temperature sensors
        self.temp_sensor1 = 25.0
        self.temp_sensor2 = 22.0
        self.temp_setpoint = 25.0

        # Pressure sensors
        self.pressure1 = 1.0  # bar
        self.pressure2 = 1.5  # bar

        # Motor/drive values
        self.motor_speed = 0.0  # RPM
        self.motor_speed_setpoint = 1500.0
        self.motor_current = 0.0  # A
        self.motor_running = False

        # Counters
        self.production_count = 0
        self.error_count = 0

        # Digital I/O
        self.input1 = False
        self.input2 = True
        self.output1 = False
        self.output2 = False

        # Analog values
        self.voltage = 24.0
        self.level = 50.0  # Tank level %

        # Energy
        self.energy_total = 0.0  # kWh

        # String values
        self.device_name = "Demo PLC"
        self.status_message = "Running"

    def update(self, dt: float) -> dict:
        """Update simulation state and return current values.

        Args:
            dt: Time delta in seconds

        Returns:
            Dictionary of current values
        """
        t = time.time() - self.start_time

        # Temperature with thermal dynamics
        self.temp_sensor1 = (
            self.temp_setpoint + 2 * math.sin(t * 0.1) + random.gauss(0, 0.2)
        )
        self.temp_sensor2 = (
            self.temp_setpoint - 3 + 1.5 * math.sin(t * 0.08) + random.gauss(0, 0.15)
        )

        # Pressure with variation
        self.pressure1 = 1.0 + 0.1 * math.sin(t * 0.05) + random.gauss(0, 0.02)
        self.pressure2 = 1.5 + 0.2 * math.sin(t * 0.07) + random.gauss(0, 0.03)

        # Motor simulation
        if self.motor_running:
            # Speed ramps toward setpoint
            speed_diff = self.motor_speed_setpoint - self.motor_speed
            self.motor_speed += speed_diff * 0.1 * dt * 10  # Ramp rate
            self.motor_speed = max(0, min(3000, self.motor_speed + random.gauss(0, 5)))
            # Current proportional to speed
            self.motor_current = (self.motor_speed / 1500) * 5.0 + random.gauss(0, 0.1)
        else:
            self.motor_speed = max(0, self.motor_speed - 100 * dt)
            self.motor_current = max(0, self.motor_current - 1 * dt)

        # Production counter (increments when motor running)
        if self.motor_running and random.random() < 0.1:
            self.production_count += 1

        # Tank level with slow change
        level_change = random.gauss(0, 0.5)
        self.level = max(0, min(100, self.level + level_change))

        # Voltage with small ripple
        self.voltage = 24.0 + 0.5 * math.sin(t * 10) + random.gauss(0, 0.1)

        # Energy accumulation
        power = (self.motor_speed / 1500) * 2.5  # kW estimate
        self.energy_total += power * dt / 3600

        # Random input changes
        if random.random() < 0.02:
            self.input1 = not self.input1
        if random.random() < 0.01:
            self.input2 = not self.input2

        # Status message based on state
        if not self.motor_running:
            self.status_message = "Motor stopped"
        elif self.motor_speed < self.motor_speed_setpoint * 0.9:
            self.status_message = "Motor ramping up"
        else:
            self.status_message = "Running"

        # Occasional error
        if random.random() < 0.001:
            self.error_count += 1

        return {
            "temp_sensor1": self.temp_sensor1,
            "temp_sensor2": self.temp_sensor2,
            "temp_setpoint": self.temp_setpoint,
            "pressure1": self.pressure1,
            "pressure2": self.pressure2,
            "motor_speed": self.motor_speed,
            "motor_speed_setpoint": self.motor_speed_setpoint,
            "motor_current": self.motor_current,
            "motor_running": self.motor_running,
            "production_count": self.production_count,
            "error_count": self.error_count,
            "input1": self.input1,
            "input2": self.input2,
            "output1": self.output1,
            "output2": self.output2,
            "voltage": self.voltage,
            "level": self.level,
            "energy_total": self.energy_total,
            "device_name": self.device_name,
            "status_message": self.status_message,
        }


class SimulatorUpdater:
    """Background task that updates simulator values in the OPC-UA server."""

    def __init__(
        self,
        simulator: PLCSimulator,
        node_vars: dict,
        interval: float = 0.1,
    ) -> None:
        """Initialize updater.

        Args:
            simulator: PLCSimulator instance
            node_vars: Dictionary mapping value names to OPC-UA node variables
            interval: Update interval in seconds
        """
        self.simulator = simulator
        self.node_vars = node_vars
        self.interval = interval
        self._running = False
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        """Start background update task."""
        self._running = True
        self._task = asyncio.create_task(self._run())
        logger.info("Simulator updater started")

    async def stop(self) -> None:
        """Stop background update task."""
        self._running = False
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
        logger.info("Simulator updater stopped")

    async def _run(self) -> None:
        """Update loop."""
        last_time = time.time()

        while self._running:
            now = time.time()
            dt = now - last_time
            last_time = now

            values = self.simulator.update(dt)
            await self._update_nodes(values)

            await asyncio.sleep(self.interval)

    async def _update_nodes(self, values: dict) -> None:
        """Write simulator values to OPC-UA nodes."""
        for name, node_var in self.node_vars.items():
            if name in values:
                try:
                    await node_var.write_value(values[name])
                except Exception as e:
                    logger.debug(f"Error updating {name}: {e}")


def _nodeid(idx: int, name: str) -> ua.NodeId:
    """Create a string NodeId for the demo namespace."""
    return ua.NodeId(name, idx)


async def create_demo_server(
    host: str = "127.0.0.1",
    port: int = 4840,
) -> tuple[Server, PLCSimulator, SimulatorUpdater, dict]:
    """Create and configure the demo OPC-UA server.

    Args:
        host: Server bind address
        port: Server port

    Returns:
        Tuple of (server, simulator, updater, node_vars)
    """
    server = Server()
    await server.init()

    # Set endpoint
    server.set_endpoint(f"opc.tcp://{host}:{port}/freeopcua/server/")
    server.set_server_name("Zelos Demo PLC Server")

    # Register namespace
    idx = await server.register_namespace(DEMO_NAMESPACE)

    # Get Objects node
    objects = server.nodes.objects

    # Create device object
    device = await objects.add_object(idx, "DemoPLC")

    # Node variables dictionary - use explicit string NodeIds
    node_vars = {}

    # Temperature folder
    temp_folder = await device.add_folder(idx, "Temperature")
    node_vars["temp_sensor1"] = await temp_folder.add_variable(
        _nodeid(idx, "Temperature.Sensor1"), "Sensor1", 25.0, ua.VariantType.Float
    )
    node_vars["temp_sensor2"] = await temp_folder.add_variable(
        _nodeid(idx, "Temperature.Sensor2"), "Sensor2", 22.0, ua.VariantType.Float
    )
    node_vars["temp_setpoint"] = await temp_folder.add_variable(
        _nodeid(idx, "Temperature.Setpoint"), "Setpoint", 25.0, ua.VariantType.Float
    )
    await node_vars["temp_setpoint"].set_writable()

    # Pressure folder
    pressure_folder = await device.add_folder(idx, "Pressure")
    node_vars["pressure1"] = await pressure_folder.add_variable(
        _nodeid(idx, "Pressure.Sensor1"), "Sensor1", 1.0, ua.VariantType.Float
    )
    node_vars["pressure2"] = await pressure_folder.add_variable(
        _nodeid(idx, "Pressure.Sensor2"), "Sensor2", 1.5, ua.VariantType.Float
    )

    # Motor folder
    motor_folder = await device.add_folder(idx, "Motor")
    node_vars["motor_speed"] = await motor_folder.add_variable(
        _nodeid(idx, "Motor.Speed"), "Speed", 0.0, ua.VariantType.Float
    )
    node_vars["motor_speed_setpoint"] = await motor_folder.add_variable(
        _nodeid(idx, "Motor.SpeedSetpoint"), "SpeedSetpoint", 1500.0, ua.VariantType.Float
    )
    await node_vars["motor_speed_setpoint"].set_writable()
    node_vars["motor_current"] = await motor_folder.add_variable(
        _nodeid(idx, "Motor.Current"), "Current", 0.0, ua.VariantType.Float
    )
    node_vars["motor_running"] = await motor_folder.add_variable(
        _nodeid(idx, "Motor.Running"), "Running", False, ua.VariantType.Boolean
    )
    await node_vars["motor_running"].set_writable()

    # Counters folder
    counters_folder = await device.add_folder(idx, "Counters")
    node_vars["production_count"] = await counters_folder.add_variable(
        _nodeid(idx, "Counters.ProductionCount"), "ProductionCount", 0, ua.VariantType.UInt32
    )
    node_vars["error_count"] = await counters_folder.add_variable(
        _nodeid(idx, "Counters.ErrorCount"), "ErrorCount", 0, ua.VariantType.UInt32
    )

    # Digital I/O folder
    dio_folder = await device.add_folder(idx, "DigitalIO")
    node_vars["input1"] = await dio_folder.add_variable(
        _nodeid(idx, "DigitalIO.Input1"), "Input1", False, ua.VariantType.Boolean
    )
    node_vars["input2"] = await dio_folder.add_variable(
        _nodeid(idx, "DigitalIO.Input2"), "Input2", True, ua.VariantType.Boolean
    )
    node_vars["output1"] = await dio_folder.add_variable(
        _nodeid(idx, "DigitalIO.Output1"), "Output1", False, ua.VariantType.Boolean
    )
    await node_vars["output1"].set_writable()
    node_vars["output2"] = await dio_folder.add_variable(
        _nodeid(idx, "DigitalIO.Output2"), "Output2", False, ua.VariantType.Boolean
    )
    await node_vars["output2"].set_writable()

    # Analog folder
    analog_folder = await device.add_folder(idx, "Analog")
    node_vars["voltage"] = await analog_folder.add_variable(
        _nodeid(idx, "Analog.Voltage"), "Voltage", 24.0, ua.VariantType.Float
    )
    node_vars["level"] = await analog_folder.add_variable(
        _nodeid(idx, "Analog.TankLevel"), "TankLevel", 50.0, ua.VariantType.Float
    )

    # Energy folder
    energy_folder = await device.add_folder(idx, "Energy")
    node_vars["energy_total"] = await energy_folder.add_variable(
        _nodeid(idx, "Energy.TotalEnergy"), "TotalEnergy", 0.0, ua.VariantType.Double
    )

    # Status folder
    status_folder = await device.add_folder(idx, "Status")
    node_vars["device_name"] = await status_folder.add_variable(
        _nodeid(idx, "Status.DeviceName"), "DeviceName", "Demo PLC", ua.VariantType.String
    )
    await node_vars["device_name"].set_writable()
    node_vars["status_message"] = await status_folder.add_variable(
        _nodeid(idx, "Status.StatusMessage"), "StatusMessage", "Running", ua.VariantType.String
    )

    # Create simulator and updater
    simulator = PLCSimulator()
    updater = SimulatorUpdater(simulator, node_vars, interval=0.1)

    return server, simulator, updater, node_vars


async def run_demo_server(
    host: str = "127.0.0.1",
    port: int = 4840,
    running_flag: asyncio.Event | None = None,
) -> None:
    """Run the demo OPC-UA server.

    Args:
        host: Server bind address
        port: Server port
        running_flag: Optional event to signal shutdown
    """
    server, simulator, updater, node_vars = await create_demo_server(host, port)

    async with server:
        await updater.start()
        logger.info(f"Demo OPC-UA server started on opc.tcp://{host}:{port}")

        try:
            while True:
                if running_flag and running_flag.is_set():
                    break
                await asyncio.sleep(1)
        finally:
            await updater.stop()


def start_demo_server_thread(host: str = "127.0.0.1", port: int = 4840) -> threading.Thread:
    """Start the demo server in a background thread.

    Args:
        host: Server bind address
        port: Server port

    Returns:
        The server thread
    """

    def run_server() -> None:
        """Run the server in its own event loop."""
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        try:
            loop.run_until_complete(run_demo_server(host, port))
        except Exception as e:
            logger.error(f"Demo server error: {e}")
        finally:
            loop.close()

    thread = threading.Thread(target=run_server, daemon=True)
    thread.start()
    logger.info(f"Demo server thread started on opc.tcp://{host}:{port}")

    # Give server time to start
    time.sleep(1.0)

    return thread
