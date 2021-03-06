from player.player import Player
from othello import Othello
from lib.replaybuffer import ReplayBuffer
from collections import deque, defaultdict
import keras.backend as K
from keras import optimizers
from keras.engine.training import Model
from keras.layers import Dense, Activation, Flatten, Conv2D, BatchNormalization, Add
from keras.engine.topology import Input
from keras.losses import mean_squared_error
from keras.models import load_model
from copy import deepcopy
import numpy as np
from random import random
from time import time
import glob

class AIPlayer(Player):
    
    def __init__(self, buffer_size, sim_count, train=True, model="", tau = 1, compile=False):
        self.buffer = ReplayBuffer(buffer_size)
        self.temp_state = deque()
        self.train = train
        self.loss = 0
        self.acc = 0
        self.batch_count = 0
        self.sim_count = sim_count
        if model != "":
            self.load(model, compile)
        else:
            self.create_network()
        self.tau = tau

    @staticmethod
    def create_if_nonexistant(config):
        models = glob.glob(config.data.model_location + "*.h5")
        if len(models) == 0:
            ai = AIPlayer(config.buffer_size, config.game.simulation_num_per_move)
            ai.save(config.data.model_location + str(time()) + ".h5")
            del ai

    def set_training(self, train):
        self.train = train
    
    @staticmethod
    def clear():
        K.clear_session()
    
    def load(self, file, compile=False):
        try:
            del self.network
        except Exception:
            pass
        self.network = load_model(file, custom_objects={"objective_function_for_policy":AIPlayer.objective_function_for_policy,
                                                        "objective_function_for_value":AIPlayer.objective_function_for_value}, compile=compile)
        
    def save(self, file):
        self.network.save(file)
    
    def create_network(self):
        x_in = Input((3, 8, 8))
        x = Conv2D(filters=128, kernel_size=(3,3), padding="same", data_format="channels_first")(x_in)
        x = BatchNormalization(axis=1)(x)
        x = Activation("relu")(x)
        for _ in range(2):
            x = self._build_residual_block(x)

        res_out = x
        
        x = Conv2D(filters=2, kernel_size=1, data_format="channels_first")(res_out)
        x = BatchNormalization(axis=1)(x)
        x = Activation("relu")(x)
        x = Flatten()(x)
        policy_out = Dense(8*8+1, activation="softmax", name="policy_out")(x)

        x = Conv2D(filters=1, kernel_size=1, data_format="channels_first")(res_out)
        x = BatchNormalization(axis=1)(x)
        x = Activation("relu")(x)
        x = Flatten()(x)
        x = Dense(64, activation="relu")(x)
        value_out =  Dense(1, activation="tanh", name="value_out")(x)
        
        self.network = Model(x_in, [policy_out, value_out], name="reversi_model")
        self.compile()
      
    def _build_residual_block(self, x):
        in_x = x
        x = Conv2D(filters=128, kernel_size=(3,3), padding="same", data_format="channels_first")(x)
        x = BatchNormalization(axis=1)(x)
        x = Activation("relu")(x)
        x = Conv2D(filters=128, kernel_size=(3,3), padding="same", data_format="channels_first")(x)
        x = BatchNormalization(axis=1)(x)
        x = Add()([in_x, x])
        x = Activation("relu")(x)
        return x
        
    def compile(self):
        losses = [AIPlayer.objective_function_for_policy, AIPlayer.objective_function_for_value]
        self.network.compile(optimizer=optimizers.SGD(lr=1e-3, momentum=0.9), loss=losses)
      
    def update_lr(self, lr):
         K.set_value(self.network.optimizer.lr, lr)
        
    @staticmethod
    def objective_function_for_policy(y_true, y_pred):
        # can use categorical_crossentropy??
        return K.sum(-y_true * K.log(y_pred + K.epsilon()), axis=-1)

    @staticmethod
    def objective_function_for_value(y_true, y_pred):
        return mean_squared_error(y_true, y_pred)
        
    def update_buffer(self, winner):
        if self.train:
            while len(self.temp_state) > 0:
                t = self.temp_state.pop()
                self.buffer.add((t[0], t[1], winner))
    
    def train_batches(self, batch_size, batches=-1, verbose=2):
        if batches == -1:
            s_buffer = np.array([_[0] for _ in self.buffer.buffer])
            p_buffer = np.array([_[1] for _ in self.buffer.buffer])
            v_buffer = np.array([_[2] for _ in self.buffer.buffer])
        else:
            sample_size = batch_size*batches
            sample = []
            while sample_size > 0:
                sample += self.buffer.sample(sample_size)
                sample_size -= self.buffer.size()
            s_buffer = np.array([_[0] for _ in sample])
            p_buffer = np.array([_[1] for _ in sample])
            v_buffer = np.array([_[2] for _ in sample])
        history = self.network.fit(s_buffer, [p_buffer, v_buffer], batch_size=batch_size, epochs=1, verbose=verbose)
        return history
    
    def preprocess_input(self, board, side):
        state = np.zeros((3, 8, 8), dtype=np.int)
        for i in range(8):
            for j in range(8):
                if board[i,j] == 1:
                    state[0,i,j] = 1
                elif board[i,j] == -1:
                    state[1,i,j] = 1
                if side == 1:
                    state[2,i,j] = 1
        return state
    
    def evaluate(self, game, side):
        current_input = self.preprocess_input(game.board, side)
        pred = self.network.predict(current_input[np.newaxis,:])
        return pred[1][0]
    
    def pick_move(self, game, side):
        possible_moves = game.possible_moves(side)
        if len(possible_moves) == 0:
            possible_moves.append((-1,-1))
        monte_prob = self.monte_carlo(game, side)
        
        if self.train:
            self.temp_state.append((self.preprocess_input(game.board, side), np.divide(monte_prob, np.sum(monte_prob))))
        
        monte_prob = np.float_power(monte_prob, 1/self.tau)
        monte_prob = np.divide(monte_prob, np.sum(monte_prob))
        
        r = random()
        for i, move in enumerate(possible_moves):
            r -= monte_prob[Othello.move_id(move)]
            if r <= 0:
                return move
        return possible_moves[-1]
            
    def monte_carlo(self, game, side):
        N = defaultdict(lambda: 0)
        W = defaultdict(lambda: 0)
        Q = defaultdict(lambda: 0)
        P = defaultdict(lambda: 0)
        
        
        possible_moves = game.possible_moves(side)
        if len(possible_moves) == 0:
            policy = np.zeros((65))
            policy[64] = 1
            return policy
        elif len(possible_moves) == 1:
            policy = np.zeros((65))
            policy[Othello.move_id(possible_moves[0])] = 1
            return policy
        
        current_input = self.preprocess_input(game.board, side)
        sid = Othello.state_id(game.board)
        pred = self.network.predict(current_input[np.newaxis,:])
        policy = pred[0][0]
        
        total = 1e-10
        for i, move in enumerate(possible_moves):
            total += policy[Othello.move_id(move)]
          
        for move in possible_moves:
            P[(sid, Othello.move_id(move))] = policy[Othello.move_id(move)]/total
        
        for i in range(self.sim_count):
            #print("Sim #%d"% i)
            clone = deepcopy(game)
            current_side = side
            visited = deque()
            while True:
                possible_moves = clone.possible_moves(current_side)
                if len(possible_moves) == 0:
                    possible_moves.append((-1,-1))
                best_move = None
                best_move_value = -2
                sid = Othello.state_id(clone.board)
                for move in possible_moves:
                    mid = Othello.move_id(move)
                    qu_val = Q[(sid, mid)] + P[(sid, mid)]/(N[(sid, mid)]+1)
                    if qu_val > best_move_value:
                        best_move_value = qu_val
                        best_move = move
                
                #print(best_move)
                
                if N[(sid, Othello.move_id(best_move))] == 0:
                    visited.append((sid, Othello.move_id(best_move)))
                    clone.play_move(best_move[0], best_move[1], current_side)
                    current_side *= -1
                    if clone.game_over():
                        for node in visited:
                            N[node] += 1
                            W[node] += clone.get_winner()*side
                            Q[node] = W[node]/N[node]
                        break
                    
                    current_input = self.preprocess_input(clone.board, current_side)
                    sid = Othello.state_id(clone.board)
                    pred = self.network.predict(current_input[np.newaxis,:])
                    policy = pred[0][0]
                    value = pred[1][0]
                    
                    possible_moves = clone.possible_moves(current_side)
                    if len(possible_moves) == 0:
                        possible_moves.append((-1,-1))
                    total = 1e-10
                    for i, move in enumerate(possible_moves):
                        total += policy[Othello.move_id(move)]
                      
                    for move in possible_moves:
                        P[(sid, Othello.move_id(move))] = policy[Othello.move_id(move)]/total
                    
                    for node in visited:
                        N[node] += 1
                        W[node] += value*side
                        Q[node] = W[node]/N[node]
                    #print()
                    break
                else:
                    visited.append((sid, Othello.move_id(best_move)))
                    clone.play_move(best_move[0], best_move[1], current_side)
                    current_side *= -1
                    if clone.game_over():
                        for node in visited:
                            N[node] += 1
                            W[node] += clone.get_winner()*side
                            Q[node] = W[node]/N[node]
                        break
                             
        policy = np.zeros((65))
        possible_moves = game.possible_moves(side)
        sid = Othello.state_id(game.board)
        for move in possible_moves:
            mid = Othello.move_id(move)
            policy[mid] = N[(sid,mid)]
        
        return policy