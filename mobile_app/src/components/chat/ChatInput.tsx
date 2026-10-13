import { Ionicons } from '@expo/vector-icons';
import { useEffect, useState } from 'react';
import { Keyboard, Platform, Pressable, TextInput, View } from 'react-native';
import { useSafeAreaInsets } from 'react-native-safe-area-context';

type Props = {
  onSend: (text: string) => void;
  /** Blocks sending (e.g. while the assistant is still answering); typing stays possible. */
  sendDisabled?: boolean;
};

const MAX_INPUT_HEIGHT = 132; // ~5 lines, then the field scrolls

/** True while the on-screen keyboard is up (the home-indicator inset is only needed when it is not). */
function useKeyboardVisible(): boolean {
  const [visible, setVisible] = useState(false);
  useEffect(() => {
    const showEvent = Platform.OS === 'ios' ? 'keyboardWillShow' : 'keyboardDidShow';
    const hideEvent = Platform.OS === 'ios' ? 'keyboardWillHide' : 'keyboardDidHide';
    const show = Keyboard.addListener(showEvent, () => setVisible(true));
    const hide = Keyboard.addListener(hideEvent, () => setVisible(false));
    return () => {
      show.remove();
      hide.remove();
    };
  }, []);
  return visible;
}

/** Message composer pinned to the bottom: auto-growing field + round accent send button. */
export function ChatInput({ onSend, sendDisabled = false }: Props) {
  const [text, setText] = useState('');
  const [focused, setFocused] = useState(false);
  const insets = useSafeAreaInsets();
  const keyboardVisible = useKeyboardVisible();

  const trimmed = text.trim();
  const canSend = trimmed.length > 0 && !sendDisabled;

  function send() {
    if (!canSend) return;
    onSend(trimmed);
    setText('');
  }

  return (
    <View
      className="border-t border-slate-800 bg-slate-900 px-3 pt-3"
      style={{ paddingBottom: keyboardVisible ? 12 : Math.max(insets.bottom, 12) }}>
      <View className="flex-row items-end">
        <TextInput
          value={text}
          onChangeText={setText}
          onFocus={() => setFocused(true)}
          onBlur={() => setFocused(false)}
          placeholder="Сообщение…"
          placeholderTextColor="#94a3b8"
          selectionColor="#60a5fa"
          cursorColor="#60a5fa"
          multiline
          textAlignVertical="center"
          className={`mr-2 flex-1 rounded-3xl border bg-slate-800 px-4 py-3 text-base leading-5 text-slate-50 ${
            focused ? 'border-blue-500' : 'border-slate-700'
          }`}
          style={{ maxHeight: MAX_INPUT_HEIGHT }}
        />
        <Pressable
          onPress={send}
          disabled={!canSend}
          hitSlop={6}
          accessibilityRole="button"
          accessibilityLabel="Отправить"
          accessibilityState={{ disabled: !canSend }}
          className={`h-12 w-12 items-center justify-center rounded-full active:scale-95 ${
            canSend ? 'bg-blue-600 active:bg-blue-700' : 'bg-slate-700'
          }`}>
          <Ionicons name="arrow-up" size={22} color={canSend ? '#ffffff' : '#94a3b8'} />
        </Pressable>
      </View>
    </View>
  );
}
