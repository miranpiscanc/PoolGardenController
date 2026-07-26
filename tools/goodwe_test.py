import asyncio
import goodwe


async def main():
    try:
        print("Connecting to inverter with DTLS...")

        inverter = await goodwe.connect(
            "192.168.200.200"
        )

        print("Connected!\n")

        data = await inverter.read_runtime_data()

        print("===== GOODWE DATA =====\n")

        for sensor in inverter.sensors():
            value = data.get(sensor.id_)
            print(f"{sensor.id_:30} {value} {sensor.unit}")

    except Exception as e:
        print("\nERROR:")
        print(type(e).__name__)
        print(e)


asyncio.run(main())
