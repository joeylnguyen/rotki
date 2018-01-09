require("./elements.js")();

var settings = null;
function Currency(name, icon, ticker_symbol, unicode_symbol) {
    this.name = name;
    this.icon = icon;
    this.ticker_symbol = ticker_symbol;
    this.unicode_symbol = unicode_symbol;
}

function assert_exchange_exists(name) {
    if (EXCHANGES.indexOf(name) < 0) {
        throw "Invalid exchange name: " + name;
    }
}

let exchanges = ['kraken', 'poloniex', 'bittrex'];
let currencies = [
    new Currency("United States Dollar", "fa-usd", "USD", "$"),
    new Currency("Euro", "fa-eur", "EUR", "€"),
    new Currency("British Pound", "fa-gbp", "GBP", "£"),
    new Currency("Japanese Yen", "fa-jpy", "JPY", "¥"),
    new Currency("Chinese Yuan", "fa-jpy", "CNY", "¥"),
];


function add_listeners() {
    $('#settingssubmit').click(function(event) {
        event.preventDefault();
        settings.floating_precision = $('#floating_precision').val();
        settings.historical_data_start_date = $('#historical_data_start_date').val();
        let main_currency = $('#maincurrencyselector').val();
        for (let i = 0; i < settings.CURRENCIES.length; i++) {
            if (main_currency == settings.CURRENCIES[i].ticker_symbol) {
                settings.main_currency = settings.CURRENCIES[i];
            }
        }

        let send_payload = {
                "ui_floating_precision": settings.floating_precision,
                "historical_data_start_date": settings.historical_data_start_date,
                "main_currency": main_currency

        };
        console.log(send_payload);
        // and now send the data to the python process
        client.invoke(
            "set_settings",
            send_payload,
            (error, res) => {
                if (error || res == null) {
                    console.log("Error at setting settings: " + error);
                } else {
                    console.log("Set settings returned " + res);
                }
        });
    });

    $('#historicaldatastart').datepicker();
}

function create_settings_ui() {
    var str = '<div class="row"> <div class="col-lg-12"><h1 class="page-header">Settings</h1></div></div>';
    str += '<div class="row"><div class="col-lg-12"><div class="panel panel-default"><div class="panel-heading">General Settings</div><div class="panel-body"></div></div></div></div>';
    $('#page-wrapper').html(str);

    str = '<div class="row"><form role="form"><div class="form-group input-group"><span class="input-group-addon">Floating Precision</span><input id="floating_precision" class="form-control" value="'+settings.floating_precision+'"type="text"></div>';
    str += '<div class="form-group input-group"><span class="input-group-addon">Date:</span><input id="historical_data_start_date" class="form-control"  value="'+settings.historical_data_start_date+'"type="text"></div>';
    str += '<div class="form-group"><label>Select Main Currency</label><select id="maincurrencyselector" class="form-control" style="font-family: \'FontAwesome\', \'sans-serif\';"></select></div>';
    $(str).appendTo($('.panel-body'));

    for (let i = 0; i < settings.CURRENCIES.length; i ++) {
        var option = '<option';
        if (settings.CURRENCIES[i] == settings.main_currency) {
            option += ' selected="selected"';
        }
        option += ' value="'+settings.CURRENCIES[i].ticker_symbol+'">'+settings.CURRENCIES[i].unicode_symbol +' - '+ settings.CURRENCIES[i].ticker_symbol+'</option>';
        $(option).appendTo($('#maincurrencyselector'));
    }

    str = form_button('Save', 'settingssubmit');
    str += '</form></div>';
    $(str).appendTo($('.panel-body'));
}

function create_or_reload_settings() {
    change_location('settings');
    if (!settings.page_settings) {
        console.log("At create/reload settings, with a null page index");
        create_settings_ui();
    } else {
        console.log("At create/reload settings, with a Populated page index");
        $('#page-wrapper').html(settings.page_settings);
    }
    add_listeners();
}


module.exports = function() {
    if (!settings) {
        settings = {};
        settings.EXCHANGES = exchanges;
        settings.CURRENCIES = currencies;
        settings.default_currency = currencies[0];
        settings.main_currency = currencies[0];
        settings.floating_precision = 2;
        settings.historical_data_start_date = "01/08/2015";
        settings.current_location = null;
        settings.page_index = null;
        settings.page_settings = null;
        settings.page_otctrades = null;
        settings.page_exchange = {};
    }
    this.assert_exchange_exists = assert_exchange_exists;
    this.create_or_reload_settings = create_or_reload_settings;

    return settings;
};
