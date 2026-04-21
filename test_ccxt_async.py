import ccxt.async_support as ccxt_async
import asyncio

async def main():
    ex = ccxt_async.binance()
    print(type(ex))
    await ex.close()

if __name__ == "__main__":
    asyncio.run(main())
