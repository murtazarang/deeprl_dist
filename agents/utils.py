import numpy as np
import tensorflow as tf

"""
initializers
"""
DEFAULT_SCALE = np.sqrt(2)
DEFAULT_MODE = 'fan_in'

def ortho_init(scale=DEFAULT_SCALE, mode=None):
    def _ortho_init(shape, dtype, partition_info=None):
        # lasagne ortho init for tf
        shape = tuple(shape)
        if len(shape) == 2: # fc: in, out
            flat_shape = shape
        elif (len(shape) == 3) or (len(shape) == 4): # 1d/2dcnn: (in_h), in_w, in_c, out
            flat_shape = (np.prod(shape[:-1]), shape[-1])
        a = np.random.standard_normal(flat_shape)
        u, _, v = np.linalg.svd(a, full_matrices=False)
        q = u if u.shape == flat_shape else v # pick the one with the correct shape
        q = q.reshape(shape)
        return (scale * q).astype(np.float32)
    return _ortho_init


def norm_init(scale=DEFAULT_SCALE, mode=DEFAULT_MODE):
    def _norm_init(shape, dtype, partition_info=None):
        shape = tuple(shape)
        if len(shape) == 2:
            n_in = shape[0]
        elif (len(shape) == 3) or (len(shape) == 4):
            n_in = np.prod(shape[:-1])
        a = np.random.standard_normal(shape)
        if mode == 'fan_in':
            n = n_in
        elif mode == 'fan_out':
            n = shape[-1]
        elif mode == 'fan_avg':
            n = 0.5 * (n_in + shape[-1])
        return (scale * a / np.sqrt(n)).astype(np.float32)

DEFAULT_METHOD = ortho_init
"""
layers
"""
def conv(x, scope, n_out, f_size, stride=1, pad='VALID', f_size_w=None, act=tf.nn.relu,
         conv_dim=1, init_scale=DEFAULT_SCALE, init_mode=None, init_method=DEFAULT_METHOD):
    with tf.variable_scope(scope):
        b = tf.get_variable("b", [n_out], initializer=tf.constant_initializer(0.0))
        if conv_dim == 1:
            n_c = x.shape[2].value
            w = tf.get_variable("w", [f_size, n_c, n_out],
                                initializer=init_method(init_scale, init_mode))
            z = tf.nn.conv1d(x, w, stride=stride, padding=pad) + b
        elif conv_dim == 2:
            n_c = x.shape[3].value
            if f_size_w is None:
                f_size_w = f_size
            w = tf.get_variable("w", [f_size, f_size_w, n_c, n_out],
                                initializer=init_method(init_scale, init_mode))
            z = tf.nn.conv2d(x, w, strides=[1, stride, stride, 1], padding=pad) + b
        return act(z)


def fc(x, scope, n_out, act=tf.nn.relu, init_scale=DEFAULT_SCALE,
       init_mode=DEFAULT_MODE, init_method=DEFAULT_METHOD):
    with tf.variable_scope(scope):
        n_in = x.shape[1].value
        w = tf.get_variable("w", [n_in, n_out],
                            initializer=init_method(init_scale, init_mode))
        b = tf.get_variable("b", [n_out], initializer=tf.constant_initializer(0.0))
        z = tf.matmul(x, w) + b
        return act(z)


def batch_to_seq(x):
    n_step = x.shape[0].value
    if len(x.shape) == 1:
        x = tf.expand_dims(x, -1)
    return tf.split(axis=0, num_or_size_splits=n_step, value=x)


def seq_to_batch(x):
    return tf.concat(x, axis=0)


def lstm(xs, dones, s, scope, init_scale=DEFAULT_SCALE, init_mode=DEFAULT_MODE,
         init_method=DEFAULT_METHOD):
    xs = batch_to_seq(xs)
    # need dones to reset states
    dones = batch_to_seq(dones)
    n_in = xs[0].shape[1].value
    n_out = s.shape[0] // 2
    with tf.variable_scope(scope):
        wx = tf.get_variable("wx", [n_in, n_out*4],
                             initializer=init_method(init_scale, init_mode))
        wh = tf.get_variable("wh", [n_out, n_out*4],
                             initializer=init_method(init_scale, init_mode))
        b = tf.get_variable("b", [n_out*4], initializer=tf.constant_initializer(0.0))
    s = tf.expand_dims(s, 0)
    c, h = tf.split(axis=1, num_or_size_splits=2, value=s)
    for ind, (x, done) in enumerate(zip(xs, dones)):
        c = c * (1-done)
        h = h * (1-done)
        z = tf.matmul(x, wx) + tf.matmul(h, wh) + b
        i, f, o, u = tf.split(axis=1, num_or_size_splits=4, value=z)
        i = tf.nn.sigmoid(i)
        f = tf.nn.sigmoid(f)
        o = tf.nn.sigmoid(o)
        u = tf.tanh(u)
        c = f*c + i*u
        h = o*tf.tanh(c)
        xs[ind] = h
    s = tf.concat(axis=1, values=[c, h])
    return seq_to_batch(xs), tf.squeeze(s)


def lstm_comm(xs, ps, dones, masks, s, scope, init_scale=DEFAULT_SCALE, init_mode=DEFAULT_MODE,
              init_method=DEFAULT_METHOD):
    n_agent = s.shape[0]
    n_h = s.shape[1] // 2
    n_s = xs.shape[-1]
    n_a = ps.shape[-1]
    xs = tf.transpose(xs, perm=[1,0,2]) # TxNxn_s
    xs = batch_to_seq(xs)
    ps = tf.transpose(ps, perm=[1,0,2]) # TxNxn_a
    ps = batch_to_seq(ps)
    # need dones to reset states
    dones = batch_to_seq(dones) # Tx1
    # create wts
    n_in_msg = n_h + n_s + n_a
    w_msg = []
    b_msg = []
    wx_hid = []
    wh_hid = []
    b_hid = []
    for i in range(n_agent):
        n_m = np.sum(masks[i])
        n_in_hid = n_s + n_h*n_m
        with tf.variable_scope(scope + ('_%d' % i)):
            w_msg.append(tf.get_variable("w_msg", [n_in_msg, n_h],
                                         initializer=init_method(init_scale, init_mode)))
            b_msg.append(tf.get_variable("b_msg", [n_h],
                                         initializer=tf.constant_initializer(0.0)))
            wx_hid.append(tf.get_variable("wx_hid", [n_in_hid, n_h*4],
                                          initializer=init_method(init_scale, init_mode)))
            wh_hid.append(tf.get_variable("wh_hid", [n_h, n_h*4],
                                          initializer=init_method(init_scale, init_mode)))
            b_hid.append(tf.get_variable("b_hid", [n_h*4],
                                         initializer=tf.constant_initializer(0.0)))
    c, h = tf.split(axis=1, num_or_size_splits=2, value=s)
    # loop over steps
    for t, (x, p, done) in enumerate(zip(xs, ps, dones)):
        # abuse 1 agent as 1 step
        x = batch_to_seq(tf.squeeze(x, axis=0))
        p = batch_to_seq(tf.squeeze(p, axis=0))
        out_h = []
        out_c = []
        out_m = []
        # communication phase
        for i, (xi, pi) in enumerate(zip(x, p)):
            hi = tf.expand_dims(h[i], axis=0)
            si = tf.concat([hi, xi, pi], axis=1)
            mi = tf.nn.relu(tf.matmul(si, w_msg[i]) + b_msg[i])
            out_m.append(mi)
        out_m = tf.concat(out_m, axis=0) # Nxn_h
        # hidden phase
        for i, xi in enumerate(x):
            ci = tf.expand_dims(c[i], axis=0)
            hi = tf.expand_dims(h[i], axis=0)
            # reset states for a new episode
            ci = ci * (1-done)
            hi = hi * (1-done)
            # receive neighbor messages
            mi = tf.expand_dims(tf.reshape(tf.boolean_mask(out_m, masks[i]), [-1]), axis=0)
            si = tf.concat([xi, mi], axis=1)
            zi = tf.matmul(si, wx_hid[i]) + tf.matmul(hi, wh_hid[i]) + b_hid[i]
            ii, fi, oi, ui = tf.split(axis=1, num_or_size_splits=4, value=zi)
            ii = tf.nn.sigmoid(ii)
            fi = tf.nn.sigmoid(fi)
            oi = tf.nn.sigmoid(oi)
            ui = tf.tanh(ui)
            ci = fi*ci + ii*ui
            hi = oi*tf.tanh(ci)
            out_h.append(hi)
            out_c.append(ci)
        c = tf.concat(out_c, axis=0)
        h = tf.concat(out_h, axis=0)
        xs[t] = tf.expand_dims(h, axis=0)
    s = tf.concat(axis=1, values=[c, h])
    print(xs[0].shape)
    xs = seq_to_batch(xs) # TxNxn_h
    print(xs.shape)
    xs = tf.transpose(xs, perm=[1,0,2]) # NxTxn_h
    return xs, s


"""
buffers
"""
class TransBuffer:
    def reset(self):
        self.buffer = []

    @property
    def size(self):
        return len(self.buffer)

    def add_transition(self, ob, a, r, *_args, **_kwargs):
        raise NotImplementedError()

    def sample_transition(self, *_args, **_kwargs):
        raise NotImplementedError()


class OnPolicyBuffer(TransBuffer):
    def __init__(self, gamma, alpha, distance_mask):
        self.gamma = gamma
        self.alpha = alpha
        if alpha > 0:
            self.distance_mask = distance_mask
            self.max_distance = np.max(distance_mask, axis=-1)
        self.reset()

    def reset(self, done=False):
        # the done before each step is required
        self.obs = []
        self.acts = []
        self.rs = []
        self.vs = []
        self.adds = []
        self.dones = [done]

    def add_transition(self, ob, na, a, r, v, done):
        self.obs.append(ob)
        self.adds.append(na)
        self.acts.append(a)
        self.rs.append(r)
        self.vs.append(v)
        self.dones.append(done)

    def sample_transition(self, R, dt=0):
        if self.alpha < 0:
            self._add_R_Adv(R)
        else:
            self._add_st_R_Adv(R, dt)
        obs = np.array(self.obs, dtype=np.float32)
        nas = np.array(self.adds, dtype=np.int32)
        acts = np.array(self.acts, dtype=np.int32)
        Rs = np.array(self.Rs, dtype=np.float32)
        Advs = np.array(self.Advs, dtype=np.float32)
        # use pre-step dones here
        dones = np.array(self.dones[:-1], dtype=np.bool)
        self.reset(self.dones[-1])
        return obs, nas, acts, dones, Rs, Advs

    def _add_R_Adv(self, R):
        Rs = []
        Advs = []
        # use post-step dones here
        for r, v, done in zip(self.rs[::-1], self.vs[::-1], self.dones[:0:-1]):
            R = r + self.gamma * R * (1.-done)
            Adv = R - v
            Rs.append(R)
            Advs.append(Adv)
        Rs.reverse()
        Advs.reverse()
        self.Rs = Rs
        self.Advs = Advs

    def _add_st_R_Adv(self, R, dt):
        Rs = []
        Advs = []
        # use post-step dones here
        tdiff = dt
        for r, v, done in zip(self.rs[::-1], self.vs[::-1], self.dones[:0:-1]):
            R = self.gamma * R * (1.-done)
            if done:
                tdiff = 0
            # additional spatial rewards
            tmax = min(tdiff, self.max_distance)
            for t in range(tmax + 1):
                rt = np.sum(r[self.distance_mask == t])
                R += (self.gamma * self.alpha) ** t * rt
            Adv = R - v
            tdiff += 1
            Rs.append(R)
            Advs.append(Adv)
        Rs.reverse()
        Advs.reverse()
        self.Rs = Rs
        self.Advs = Advs

    def _add_s_R_Adv(self, R):
        Rs = []
        Advs = []
        # use post-step dones here
        for r, v, done in zip(self.rs[::-1], self.vs[::-1], self.dones[:0:-1]):
            R = self.gamma * R * (1.-done)
            # additional spatial rewards
            for t in range(self.max_distance + 1):
                rt = np.sum(r[self.distance_mask == t])
                R += (self.alpha) ** t * rt
            Adv = R - v
            Rs.append(R)
            Advs.append(Adv)
        Rs.reverse()
        Advs.reverse()
        self.Rs = Rs
        self.Advs = Advs


class MultiAgentOnPolicyBuffer(OnPolicyBuffer):
    def __init__(self, gamma, alpha, distance_mask):
        super().__init__(gamma, alpha, distance_mask)

    def sample_transition(self, R):
        if self.alpha < 0:
            self._add_R_Adv(R)
        else:
            self._add_st_R_Adv(R)
        obs = np.transpose(np.array(self.obs, dtype=np.float32), (1, 0, 2))
        policies = np.transpose(np.array(self.adds, dtype=np.float32), (1, 0, 2))
        acts = np.transpose(np.array(self.acts, dtype=np.int32))
        Rs = np.array(self.Rs, dtype=np.float32)
        Advs = np.array(self.Advs, dtype=np.float32)
        dones = np.array(self.dones[:-1], dtype=np.bool)
        self.reset(self.dones[-1])
        return obs, policies, acts, dones, Rs, Advs

    def _add_R_Adv(self, R):
        Rs = []
        Advs = []
        vs = np.array(self.vs)
        for i in range(vs.shape[1]):
            cur_Rs = []
            cur_Advs = []
            cur_R = R[i]
            for r, v, done in zip(self.rs[::-1], vs[::-1,i], self.dones[:0:-1]):
                cur_R = r + self.gamma * cur_R * (1.-done)
                cur_Adv = cur_R - v
                cur_Rs.append(cur_R)
                cur_Advs.append(cur_Adv)
            cur_Rs.reverse()
            cur_Advs.reverse()
            Rs.append(cur_Rs)
            Advs.append(cur_Advs)
        self.Rs = np.array(Rs)
        self.Advs = np.array(Advs)

    def _add_st_R_Adv(self, R):
        Rs = []
        Advs = []
        vs = np.array(self.vs)
        for i in range(vs.shape[1]):
            cur_Rs = []
            cur_Advs = []
            cur_R = R[i]
            tdiff = 0
            distance_mask = self.distance_mask[i]
            max_distance = self.max_distance[i]
            for r, v, done in zip(self.rs[::-1], vs[::-1,i], self.dones[:0:-1]):
                cur_R = self.gamma * cur_R * (1.-done)
                if done:
                    tdiff = 0
                # additional spatial rewards
                tmax = min(tdiff, max_distance)
                for t in range(tmax + 1):
                    rt = np.sum(r[distance_mask==t])
                    cur_R += (self.gamma * self.alpha) ** t * rt
                cur_Adv = cur_R - v
                cur_Rs.append(cur_R)
                cur_Advs.append(cur_Adv)
            cur_Rs.reverse()
            cur_Advs.reverse()
            Rs.append(cur_Rs)
            Advs.append(cur_Advs)
        self.Rs = np.array(Rs)
        self.Advs = np.array(Advs)

"""
util functions
"""
class Scheduler:
    def __init__(self, val_init, val_min=0, total_step=0, decay='linear'):
        self.val = val_init
        self.N = float(total_step)
        self.val_min = val_min
        self.decay = decay
        self.n = 0

    def get(self, n_step):
        self.n += n_step
        if self.decay == 'linear':
            return max(self.val_min, self.val * (1 - self.n / self.N))
        else:
            return self.val

