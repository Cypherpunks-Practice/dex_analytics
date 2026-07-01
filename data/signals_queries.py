import psycopg2

pg_connection = psycopg2.connect(
        host="localhost",
        port=5432,
        database="mydb",
        user="postgres",
        password="mysecretpassword"
)

def get_signals_df(limit = 50, min_timestamp = 0, max_timestamp = 0xffffffffffffffff, 
        timestamp_increase = None, tokens_a_list = None, tokens_b_list = None,
        min_amount = 0, max_amount = 0xffffffffffffffff, amount_increase = None,
        min_profit = 0, max_profit = 0xffffffffffffffff, profit_increase = None):
        global pg_connection
        cursor = pg_connection.cursor()

        orderby = "ORDER BY"
        if timestamp_increase == True:
                orderby+=" timestamp asc"
        elif timestamp_increase == False:
                orderby+=" timestamp desc"
        elif amount_increase == True:
                orderby+=" quote_amount asc"
        elif amount_increase == False:
                orderby+=" quote_amount desc"
        elif profit_increase == True:
                orderby+=" potential_profit asc"
        elif profit_increase == False:
                orderby+=" potential_profit desc"
        else: orderby=""

        tokens_a =  ""
        if tokens_a_list:
                tokens_a = "AND base_token IN ("+", ".join(f"'{i}'" for i in tokens_a_list)+") "
        tokens_b =  ""
        if tokens_b_list:
                tokens_b = "AND quote_token IN ("+", ".join(f"'{i}'" for i in tokens_b_list)+") "      

        cursor.execute(f'''select swaps_request.id, swaps_request.timestamp, base_token, 
                        quote_token, quote_amount, potential_profit 
                        from arbitrages join swaps_request on arbitrages.id=swaps_request.arbitrage_id 
                        where swaps_request.timestamp > {min_timestamp} and swaps_request.timestamp < {max_timestamp} and 
                        quote_amount > {min_amount} and quote_amount < {max_amount} and 
                        potential_profit > {min_profit} and potential_profit < {max_profit}
                        {tokens_a}{tokens_b}
                        {orderby} 
                        limit {limit};''')
        result = cursor.fetchall()
        cursor.close()
        return result

'''
res = get_signals_df(10, tokens_a_list=['UNI'], profit_increase=False)
for i in res:
        print(i)
'''




    
    