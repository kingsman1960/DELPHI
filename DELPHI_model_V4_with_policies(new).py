# Authors: Hamza Tazi Bouardi (htazi@mit.edu), Michael L. Li (mlli@mit.edu), Omar Skali Lami (oskali@mit.edu)
import pandas as pd
import numpy as np
from scipy.integrate import solve_ivp
from datetime import datetime, timedelta
from DELPHI_utils_V4_static import DELPHIDataCreator, DELPHIDataSaver, get_initial_conditions, compute_mape, create_fitting_data_from_validcases, get_mape_data_fitting, DELPHIAggregations
from DELPHI_utils_V4_dynamic import (
    read_oxford_international_policy_data, get_normalized_policy_shifts_and_current_policy_all_countries,
    get_normalized_policy_shifts_and_current_policy_us_only, read_policy_data_us_only
)
from DELPHI_params_V4 import (
    fitting_start_date,
    date_MATHEMATICA, validcases_threshold_policy, default_dict_normalized_policy_gamma,
    IncubeD, RecoverID, RecoverHD, DetectD, VentilatedD,
    default_maxT_policies, p_v, p_d, p_h, future_policies, future_times
)
import yaml
import os
import argparse


with open("config.yml", "r") as ymlfile:
    CONFIG = yaml.load(ymlfile, Loader=yaml.BaseLoader)
CONFIG_FILEPATHS = CONFIG["filepaths"]
yesterday = "".join(str(datetime.now().date() - timedelta(days=1)).split("-"))
parser = argparse.ArgumentParser()
parser.add_argument(
    '--run_config', '-rc', type=str, required=True,
    help="specify relative path for the run config YAML file"
)
arguments = parser.parse_args()
with open(arguments.run_config, "r") as ymlfile:
    RUN_CONFIG = yaml.load(ymlfile, Loader=yaml.BaseLoader)

USER_RUNNING = RUN_CONFIG["arguments"]["user"]
OPTIMIZER = RUN_CONFIG["arguments"]["optimizer"]
GET_CONFIDENCE_INTERVALS = bool(int(RUN_CONFIG["arguments"]["confidence_intervals"]))
SAVE_TO_WEBSITE = bool(int(RUN_CONFIG["arguments"]["website"]))
SAVE_SINCE100_CASES = bool(int(RUN_CONFIG["arguments"]["since100case"]))
PATH_TO_FOLDER_DANGER_MAP = CONFIG_FILEPATHS["danger_map"][USER_RUNNING]
PATH_TO_DATA_SANDBOX = CONFIG_FILEPATHS["data_sandbox"][USER_RUNNING]
PATH_TO_WEBSITE_PREDICTED = CONFIG_FILEPATHS["website"][USER_RUNNING]
policy_data_countries = read_oxford_international_policy_data(yesterday=yesterday)
policy_data_us_only = read_policy_data_us_only(filepath_data_sandbox=CONFIG_FILEPATHS["data_sandbox"][USER_RUNNING])
popcountries = pd.read_csv(PATH_TO_FOLDER_DANGER_MAP + f"processed/Population_Global.csv")
df_initial_states = pd.read_csv(
    PATH_TO_DATA_SANDBOX + f"predicted/raw_predictions/Predicted_model_state_V3_{fitting_start_date}.csv"
)
subname_parameters_file = None
if OPTIMIZER == "tnc":
    subname_parameters_file = "Global_V4"
elif OPTIMIZER == "annealing":
    subname_parameters_file = "Global_V4_annealing"
elif OPTIMIZER == "trust-constr":
    subname_parameters_file = "Global_V4_trust"
else:
    raise ValueError("Optimizer not supported in this implementation")
past_parameters = pd.read_csv(
    PATH_TO_FOLDER_DANGER_MAP + f"predicted/Parameters_{subname_parameters_file}_{yesterday}.csv"
)
if pd.to_datetime(yesterday) < pd.to_datetime(date_MATHEMATICA):
    param_MATHEMATICA = True
else:
    param_MATHEMATICA = False
# True if we use the Mathematica run parameters, False if we use those from Python runs
# This is because the past_parameters dataframe's columns are not in the same order in both cases
startT = fitting_start_date
# Get the policies shifts from the CART tree to compute different values of gamma(t)
# Depending on the policy in place in the future to affect predictions
dict_normalized_policy_gamma_countries, dict_current_policy_countries = (
    get_normalized_policy_shifts_and_current_policy_all_countries(
        policy_data_countries=policy_data_countries,
        past_parameters=past_parameters,
    )
)
# Setting same value for these 2 policies because of the inherent structure of the tree
dict_normalized_policy_gamma_countries[future_policies[3]] = dict_normalized_policy_gamma_countries[future_policies[5]]
# US Only Policies
dict_normalized_policy_gamma_us_only, dict_current_policy_us_only = (
    get_normalized_policy_shifts_and_current_policy_us_only(
        policy_data_us_only=policy_data_us_only,
        past_parameters=past_parameters,
    )
)
dict_current_policy_international = dict_current_policy_countries.copy()
dict_current_policy_international.update(dict_current_policy_us_only)

dict_normalized_policy_gamma_us_only = default_dict_normalized_policy_gamma
dict_normalized_policy_gamma_countries = default_dict_normalized_policy_gamma

# Initalizing lists of the different dataframes that will be concatenated in the end
list_df_global_predictions_since_today_scenarios = []
list_df_global_predictions_since_100_cases_scenarios = []
obj_value = 0
list_tuples = [(
    r.continent, 
    r.country, 
    r.province, 
    r.values[:16] if not pd.isna(r.S) else None
    ) for _, r in df_initial_states.iterrows()]
for continent, country, province, initial_state in list_tuples:
    if country == "US":  # This line is necessary because the keys are the same in both cases
        dict_normalized_policy_gamma_international = dict_normalized_policy_gamma_us_only.copy()
    else:
        dict_normalized_policy_gamma_international = dict_normalized_policy_gamma_countries.copy()

    country_sub = country.replace(" ", "_")
    province_sub = province.replace(" ", "_")
    if (
            (os.path.exists(PATH_TO_FOLDER_DANGER_MAP + f"processed/Global/Cases_{country_sub}_{province_sub}.csv"))
            and ((country, province) in dict_current_policy_international.keys())
    ):
        totalcases = pd.read_csv(
            PATH_TO_FOLDER_DANGER_MAP + f"processed/Global/Cases_{country_sub}_{province_sub}.csv"
        )
        if totalcases.day_since100.max() < 0:
            print(f"Not enough cases for Continent={continent}, Country={country} and Province={province}")
            continue
        print(country + " " + province)
        if past_parameters is not None:
            parameter_list_total = past_parameters[
                (past_parameters.Country == country) &
                (past_parameters.Province == province)
                ]
            if len(parameter_list_total) > 0:
                parameter_list_line = parameter_list_total.iloc[-1, :].values.tolist()
                if param_MATHEMATICA:
                    parameter_list = parameter_list_line[4:]
                    parameter_list[3] = np.log(2) / parameter_list[3]
                else:
                    parameter_list = parameter_list_line[5:]
                date_day_since100 = pd.to_datetime(parameter_list_line[3])
                # Allowing a 5% drift for states with past predictions, starting in the 5th position are the parameters
                start_date = max(pd.to_datetime(startT), date_day_since100)
                validcases = totalcases[
                    (totalcases.date >= str(start_date))
                    & (totalcases.date <= str((pd.to_datetime(yesterday) + timedelta(days=1)).date()))
                ][["day_since100", "case_cnt", "death_cnt"]].reset_index(drop=True)
            else:
                print(f"Must have past parameters for {country} and {province}")
                continue
        else:
            print("Must have past parameters")
            continue

        # Now we start the modeling part:
        if len(validcases) > validcases_threshold_policy:
            PopulationT = popcountries[
                (popcountries.Country == country) & (popcountries.Province == province)
            ].pop2016.iloc[-1]
            N = PopulationT
            PopulationI = validcases.loc[0, "case_cnt"]
            PopulationD = validcases.loc[0, "death_cnt"]
            if initial_state is not None:
                R_0 = initial_state[9]
            else:
                R_0 = validcases.loc[0, "death_cnt"] * 5 if validcases.loc[0, "case_cnt"] - validcases.loc[0, "death_cnt"]> validcases.loc[0, "death_cnt"] * 5 else 0
            cases_t_14days = totalcases[totalcases.date >= str(start_date- pd.Timedelta(14, 'D'))]['case_cnt'].values[0]
            deaths_t_9days = totalcases[totalcases.date >= str(start_date - pd.Timedelta(9, 'D'))]['death_cnt'].values[0]
            R_upperbound = validcases.loc[0, "case_cnt"] - validcases.loc[0, "death_cnt"]
            R_heuristic = cases_t_14days - deaths_t_9days

            """
            Fixed Parameters based on meta-analysis:
            p_h: Hospitalization Percentage
            RecoverHD: Average Days until Recovery
            VentilationD: Number of Days on Ventilation for Ventilated Patients
            maxT: Maximum # of Days Modeled
            p_d: Percentage of True Cases Detected
            p_v: Percentage of Hospitalized Patients Ventilated,
            balance: Regularization coefficient between cases and deaths
            """
            maxT = (default_maxT_policies - date_day_since100).days + 1
            t_cases = validcases["day_since100"].tolist() - validcases.loc[0, "day_since100"]
            balance, cases_data_fit, deaths_data_fit = create_fitting_data_from_validcases(validcases)
            GLOBAL_PARAMS_FIXED = (N, R_upperbound, R_heuristic, R_0, PopulationD, PopulationI, p_d, p_h, p_v)
            best_params = parameter_list
            t_predictions = [i for i in range(maxT)]
            for future_policy in future_policies:
                for future_time in future_times:
                    def model_covid_predictions(
                            t, x, alpha, days, r_s, r_dth, p_dth, r_dthdecay, k1, k2, jump, t_jump, std_normal, k3
                    ):
                        """
                        SEIR based model with 16 distinct states, taking into account undetected, deaths, hospitalized
                        and recovered, and using an ArcTan government response curve, corrected with a Gaussian jump in
                        case of a resurgence in cases
                        :param t: time step
                        :param x: set of all the states in the model (here, 16 of them)
                        :param alpha: Infection rate
                        :param days: Median day of action (used in the arctan governmental response)
                        :param r_s: Median rate of action (used in the arctan governmental response)
                        :param r_dth: Rate of death
                        :param p_dth: Initial mortality percentage
                        :param r_dthdecay: Rate of decay of mortality percentage
                        :param k1: Internal parameter 1 (used for initial conditions)
                        :param k2: Internal parameter 2 (used for initial conditions)
                        :param jump: Amplitude of the Gaussian jump modeling the resurgence in cases
                        :param t_jump: Time where the Gaussian jump will reach its maximum value
                        :param std_normal: Standard Deviation of the Gaussian jump (~ time span of resurgence in cases)
                        :return: predictions for all 16 states, which are the following
                        [0 S, 1 E, 2 I, 3 UR, 4 DHR, 5 DQR, 6 UD, 7 DHD, 8 DQD, 9 R, 10 D, 11 TH,
                        12 DVR,13 DVD, 14 DD, 15 DT]
                        """
                        r_i = np.log(2) / IncubeD  # Rate of infection leaving incubation phase
                        r_d = np.log(2) / DetectD  # Rate of detection
                        r_ri = np.log(2) / RecoverID  # Rate of recovery not under infection
                        r_rh = np.log(2) / RecoverHD  # Rate of recovery under hospitalization
                        r_rv = np.log(2) / VentilatedD  # Rate of recovery under ventilation
                        gamma_t = (
                              (2 / np.pi) * np.arctan(-(t - days) / 20 * r_s) + 1 +
                              jump * np.exp(-(t - t_jump)**2 /(2 * std_normal ** 2))
                        )
                        gamma_t_future = (
                              (2 / np.pi) * np.arctan(-(t_cases[-1] + future_time - days) / 20 * r_s) + 1 +
                              jump * np.exp(-(t_cases[-1] + future_time - t_jump)**2 / (2 * std_normal ** 2))
                        )
                        p_dth_mod = (2 / np.pi) * (p_dth - 0.01) * (np.arctan(- t / 20 * r_dthdecay) + np.pi / 2) + 0.01
                        if t > t_cases[-1] + future_time:
                            normalized_gamma_future_policy = dict_normalized_policy_gamma_countries[future_policy]
                            normalized_gamma_current_policy = dict_normalized_policy_gamma_countries[
                                dict_current_policy_international[(country, province)]
                            ]
                            epsilon = 1e-4
                            gamma_t = gamma_t + min(
                                (2 - gamma_t_future) / (1 - normalized_gamma_future_policy + epsilon),
                                (gamma_t_future / normalized_gamma_current_policy) *
                                (normalized_gamma_future_policy - normalized_gamma_current_policy)
                            )

                        assert len(x) == 16, f"Too many input variables, got {len(x)}, expected 16"
                        S, E, I, AR, DHR, DQR, AD, DHD, DQD, R, D, TH, DVR, DVD, DD, DT = x
                        # Equations on main variables
                        dSdt = -alpha * gamma_t * S * I / N
                        dEdt = alpha * gamma_t * S * I / N - r_i * E
                        dIdt = r_i * E - r_d * I
                        dARdt = r_d * (1 - p_dth_mod) * (1 - p_d) * I - r_ri * AR
                        dDHRdt = r_d * (1 - p_dth_mod) * p_d * p_h * I - r_rh * DHR
                        dDQRdt = r_d * (1 - p_dth_mod) * p_d * (1 - p_h) * I - r_ri * DQR
                        dADdt = r_d * p_dth_mod * (1 - p_d) * I - r_dth * AD
                        dDHDdt = r_d * p_dth_mod * p_d * p_h * I - r_dth * DHD
                        dDQDdt = r_d * p_dth_mod * p_d * (1 - p_h) * I - r_dth * DQD
                        dRdt = r_ri * (AR + DQR) + r_rh * DHR
                        dDdt = r_dth * (AD + DQD + DHD)
                        # Helper states (usually important for some kind of output)
                        dTHdt = r_d * p_d * p_h * I
                        dDVRdt = r_d * (1 - p_dth_mod) * p_d * p_h * p_v * I - r_rv * DVR
                        dDVDdt = r_d * p_dth_mod * p_d * p_h * p_v * I - r_dth * DVD
                        dDDdt = r_dth * (DHD + DQD)
                        dDTdt = r_d * p_d * I
                        return [
                            dSdt, dEdt, dIdt, dARdt, dDHRdt, dDQRdt, dADdt, dDHDdt, dDQDdt,
                            dRdt, dDdt, dTHdt, dDVRdt, dDVDdt, dDDdt, dDTdt
                        ]


                    def solve_best_params_and_predict(optimal_params):
                        # Variables Initialization for the ODE system
                        x_0_cases = get_initial_conditions(
                            params_fitted=optimal_params,
                            global_params_fixed=GLOBAL_PARAMS_FIXED
                        )
                        x_sol_best = solve_ivp(
                            fun=model_covid_predictions,
                            y0=x_0_cases,
                            t_span=[t_predictions[0], t_predictions[-1]],
                            t_eval=t_predictions,
                            args=tuple(optimal_params),
                        ).y
                        return x_sol_best


                    x_sol_final = solve_best_params_and_predict(best_params)
                    data_creator = DELPHIDataCreator(
                        x_sol_final=x_sol_final, date_day_since100=date_day_since100, best_params=best_params,
                        continent=continent, country=country, province=province,
                    )
                    # Creating the parameters dataset for this (Continent, Country, Province)
                    mape_data = (
                                        compute_mape(cases_data_fit, x_sol_final[15, :len(cases_data_fit)]) +
                                        compute_mape(deaths_data_fit, x_sol_final[14, :len(deaths_data_fit)])
                                ) / 2
                    try:
                        mape_data_2 = (
                                              compute_mape(cases_data_fit[-15:],
                                                   x_sol_final[15, len(cases_data_fit) - 15:len(cases_data_fit)]) +
                                              compute_mape(deaths_data_fit[-15:],
                                                   x_sol_final[14, len(deaths_data_fit) - 15:len(deaths_data_fit)])
                                      ) / 2
                    except IndexError:
                        mape_data_2 = mape_data
                    # print(
                    #     "Policy: ", future_policy, "\t Enacting Time: ", future_time, "\t Total MAPE=", mape_data,
                    #     "\t MAPE on last 15 days=", mape_data_2
                    # )
                    # print(best_params)
                    # print(country + ", " + province)
                    # if future_policy in [
                    #     'No_Measure', 'Restrict_Mass_Gatherings_and_Schools_and_Others',
                    #     'Authorize_Schools_but_Restrict_Mass_Gatherings_and_Others', 'Lockdown'
                    # ]:
                    #     future_policy_lab = " ".join(future_policy.split("_"))
                    #     n_points_to_leave = (pd.to_datetime(yesterday) - date_day_since100).days
                    #     plt.plot(t_predictions[n_points_to_leave:],
                    #              x_sol_final[15, n_points_to_leave:],
                    #              label=f"Future Policy: {future_policy_lab} in {future_time} days")
                    #Creating the datasets for predictions of this (Continent, Country, Province)
                    df_predictions_since_today_cont_country_prov, df_predictions_since_100_cont_country_prov = (
                        data_creator.create_datasets_predictions_scenario(
                            policy=future_policy,
                            time=future_time,
                            totalcases=totalcases,
                        )
                    )
                    list_df_global_predictions_since_today_scenarios.append(
                        df_predictions_since_today_cont_country_prov)
                    list_df_global_predictions_since_100_cases_scenarios.append(
                        df_predictions_since_100_cont_country_prov)
            print(f"Finished predicting for Continent={continent}, Country={country} and Province={province}")
            # plt.plot(fitcasesnd, label="Historical Data")
            # dates_values = [
            #     str((pd.to_datetime(yesterday)+timedelta(days=i)).date())[5:] if i % 10 == 0 else " "
            #     for i in range(len(x_sol_final[15, n_points_to_leave:]))
            # ]
            # plt.xticks(t_predictions[n_points_to_leave:], dates_values, rotation=90, fontsize=18)
            # plt.yticks(fontsize=18)
            # plt.legend(fontsize=18)
            # plt.title(f"{country}, {province} Predictions & Historical for # Cases")
            # plt.savefig(country + "_" + province + "_prediction_cases.png", bpi=300)
            print("--------------------------------------------------------------------------")
        else:  # len(validcases) <= 7
            print(f"Not enough historical data (less than a week)" +
                  f"for Continent={continent}, Country={country} and Province={province}")
            continue
    else:  # file for that tuple (country, province) doesn't exist in processed files
        continue

# Appending parameters, aggregations per country, per continent, and for the world
# for predictions today & since 100
today_date_str = "".join(str(datetime.now().date()).split("-"))
df_global_predictions_since_today_scenarios = pd.concat(
    list_df_global_predictions_since_today_scenarios
).reset_index(drop=True)
df_global_predictions_since_100_cases_scenarios = pd.concat(
    list_df_global_predictions_since_100_cases_scenarios
).reset_index(drop=True)
delphi_data_saver = DELPHIDataSaver(
    path_to_folder_danger_map=PATH_TO_FOLDER_DANGER_MAP,
    path_to_website_predicted=PATH_TO_WEBSITE_PREDICTED,
    df_global_parameters=None,
    df_global_predictions_since_today=df_global_predictions_since_today_scenarios,
    df_global_predictions_since_100_cases=df_global_predictions_since_100_cases_scenarios,
)
# df_global_predictions_since_100_cases_scenarios.to_csv('df_global_predictions_since_100_cases_scenarios_world.csv', index=False)
delphi_data_saver.save_policy_predictions_to_json(website=SAVE_TO_WEBSITE, local_delphi=False)
print("Exported all policy-dependent predictions for all countries to website & danger_map repositories")