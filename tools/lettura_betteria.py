import asyncio
import goodwe


async def main():
    try:
        print("Connecting to GoodWe inverter...\n")

        inverter = await goodwe.connect(
            "192.168.200.200",
            family="ET"
        )

        print("Connected!\n")

        data = await inverter.read_runtime_data()

        print("Saving complete sensor list to goodwe_sensors.txt...\n")

        with open("goodwe_sensors.txt", "w", encoding="utf-8") as f:
            for sensor in inverter.sensors():
                value = data.get(sensor.id_)
                line = f"{sensor.id_:35} {str(value):15} {sensor.unit}\n"
                f.write(line)

        print("Battery-related sensors:\n")

        found = False

        for sensor in inverter.sensors():
            sid = sensor.id_.lower()

            if (
                "battery" in sid
                or "batt" in sid
                or "soc" in sid
                or "charge" in sid
                or "discharge" in sid
            ):
                found = True
                print(f"{sensor.id_:35} {data.get(sensor.id_)} {sensor.unit}")

        if not found:
            print("No battery-related sensors found.")

        print("\nComplete sensor list saved to:")
        print("goodwe_sensors.txt")

    except Exception as e:
        print("\nERROR:")
        print(type(e).__name__)
        print(e)


asyncio.run(main())
