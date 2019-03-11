import sys
from typing import Any, Dict

from asset_aggregator.utils import choose_multiple


def name_check(
        asset_symbol: str,
        our_asset: Dict[str, Any],
        our_data: Dict[str, Any],
        paprika_data: Dict[str, Any],
        cmc_data: Dict[str, Any],
) -> Dict[str, Any]:
    """Process the name from coin paprika and coinmarketcap

    Then compare to our data and provide choices to clean up the data.
    """

    our_name = our_asset.get('name', None)
    if our_name:
        # If we already got a name from manual input then keep it
        return our_data

    paprika_name = paprika_data['name']
    cmc_name = None
    if cmc_data:
        cmc_name = cmc_data['name']

    if not paprika_name and not cmc_name:
        print('No name in any external api for asset {asset_symbol}')
        sys.exit(1)

    msg = (
        f'For asset {asset_symbol} the possible names are: \n'
        f'(1) Coinpaprika: {paprika_name}\n'
        f'(2) Coinmarketcap: {cmc_name}\n'
        f'Choose a number (1)-(2) to choose which name to use: '
    )
    choice = choose_multiple(msg, (1, 2))
    if choice == 1:
        name = paprika_name
    elif choice == 2:
        if not cmc_name:
            print("Chose coinmarketcap's name but it's empty. Bailing ...")
            sys.exit(1)
        name = cmc_name

    our_data[asset_symbol]['name'] = name
    return our_data